<script>
  import { onMount } from "svelte";
  import { getCurrentWebview } from "@tauri-apps/api/webview";
  import { getVersion } from "@tauri-apps/api/app";
  import { appCacheDir } from "@tauri-apps/api/path";
  import { open } from "@tauri-apps/plugin-dialog";
  import { Command } from "@tauri-apps/plugin-shell";
  import { revealItemInDir } from "@tauri-apps/plugin-opener";
  import { stat, readDir, readTextFile } from "@tauri-apps/plugin-fs";
  import { writeText } from "@tauri-apps/plugin-clipboard-manager";

  const OUTPUT_MODES = [
    { value: "individual", label: "Individual .md files" },
    { value: "both", label: "Individual + combined file" },
    { value: "combined-only", label: "Combined file only" },
  ];
  const DOC_EXTS = ["docx", "pdf", "pptx", "xlsx", "xls", "msg", "epub", "ipynb", "html", "htm", "vtt"];
  const MEDIA_EXT_LIST = ["mp3", "m4a", "wav", "flac", "ogg", "aac", "opus", "mp4", "mov", "webm", "mkv"];
  const MEDIA_EXTS = new Set(MEDIA_EXT_LIST);
  const SUPPORTED = new Set([...DOC_EXTS, ...MEDIA_EXT_LIST]);

  const SPEAKER_OPTIONS = [
    { value: "auto", label: "Auto-detect" },
    { value: "2", label: "2 speakers" },
    { value: "3", label: "3 speakers" },
    { value: "4+", label: "4+ speakers" },
  ];

  const DEFAULT_SETTINGS = {
    outputMode: "both",
    zip: false,
    ocr: false,
    redact: false,
    conflict: "keep-both",
    yaml: true,
    toc: true,
    engine: "regex", // "regex" | "ner" | "ollama"
    ollamaModel: "llama3",
    customTerms: "",
    revealWhenDone: true,
    diarize: true,
    speakers: "auto",
    audioModel: "",
  };

  function loadSettings() {
    try {
      return { ...DEFAULT_SETTINGS, ...JSON.parse(localStorage.getItem("docprep.settings") || "{}") };
    } catch {
      return { ...DEFAULT_SETTINGS };
    }
  }

  let s = $state(loadSettings());
  let state = $state("empty"); // empty | staged | running | done
  let items = $state([]);
  let progress = $state({ done: 0, total: 0, current: "" });
  let summary = $state(null);
  let revealTarget = $state("");
  let footerMsg = $state("");
  let showSettings = $state(false);

  // Offline speech model catalog & download state
  let modelCatalog = $state(null);
  let catalogLoading = $state(false);
  let activeDownload = $state(null); // { modelId, pct, stage, error }
  let selectedSetupModel = $state("");
  let pendingAutoConvert = $state(false);
  let setupDialogEl = $state(null);
  let pointerDownOutsideSetup = false;

  // Version comes from tauri.conf.json at runtime so the label can never drift
  // from the release again ("2.0.1-beta.3" is shown as "v2.0.1 beta 3").
  let appVersion = $state("");
  onMount(() => {
    getVersion()
      .then((v) => (appVersion = "v" + v.replace(/-(alpha|beta|rc)\.?/, " $1 ")))
      .catch(() => {});
  });

  let child = null;
  let downloadChild = null;
  // The engine records where each output went (--manifest) so "Read with OCR"
  // can re-run the same batch and overwrite exactly those files (--replace-from)
  let manifestPath = "";
  let lastArgs = null;
  onMount(() => {
    appCacheDir()
      .then((d) => (manifestPath = joinPath(d, "last-run.json")))
      .catch(() => {});
  });

  $effect(() => {
    localStorage.setItem("docprep.settings", JSON.stringify(s));
  });

  function includedFolderCount(item) {
    if (!item.isDir) return item.included === false ? 0 : 1;
    if (item.count === null) return 0;
    const excludedChildren = (item.stagedMedia || []).filter((m) => !m.included).length;
    return Math.max(0, item.count - excludedChildren);
  }

  const totalFiles = $derived(items.reduce((n, i) => n + includedFolderCount(i), 0));
  const scanning = $derived(items.some((i) => i.isDir && i.count === null));
  // Files whose images or scanned pages went unread because OCR was off
  const unreadFiles = $derived(
    items.reduce((n, i) => n + (i.isDir ? i.children.filter((c) => c.unread).length : i.unread ? 1 : 0), 0)
  );

  const hasIncludedMedia = $derived.by(() => {
    for (const i of items) {
      if (!i.isDir && i.isMedia && i.included !== false) return true;
      if (i.isDir && (i.stagedMedia || []).some((m) => m.included)) return true;
    }
    return false;
  });

  const installedModels = $derived((modelCatalog?.models || []).filter((m) => m.installed));

  const totalPct = $derived.by(() => {
    if (!summary || !(summary.src_tokens > summary.out_comparable) || !(summary.out_comparable > 0)) return null;
    const pct = Math.min(99, Math.round(100 * (1 - summary.out_comparable / summary.src_tokens)));
    return pct > 0 ? pct : null;
  });

  function rowPct(item) {
    if (!(item.src > item.outCmp) || !(item.outCmp > 0)) return null;
    const pct = Math.min(99, Math.round(100 * (1 - item.outCmp / item.src)));
    return pct > 0 ? pct : null;
  }

  onMount(() => {
    getCurrentWebview().onDragDropEvent((event) => {
      if (event.payload.type === "drop" && state !== "running") {
        addPaths(event.payload.paths);
      }
    });
  });

  function baseName(p) {
    return p.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  }
  function extOf(name) {
    const i = name.lastIndexOf(".");
    return i > 0 ? name.slice(i + 1).toLowerCase() : "";
  }
  function isMediaName(name) {
    return MEDIA_EXTS.has(extOf(name));
  }
  function joinPath(dir, name) {
    const sep = dir.includes("\\") ? "\\" : "/";
    return dir.replace(/[\\/]+$/, "") + sep + name;
  }

  function formatDuration(sec) {
    if (!(sec > 0)) return "";
    const total = Math.max(1, Math.round(sec));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const sRem = total % 60;
    if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m > 0) return `${m}m ${String(sRem).padStart(2, "0")}s`;
    return `${sRem}s`;
  }

  function formatStageLabel(stage, pct) {
    const suffix = pct > 0 && pct < 100 ? ` ${pct}%…` : "…";
    if (stage === "decoding") return "extracting audio…";
    if (stage === "diarizing") return `identifying speakers${suffix}`;
    if (stage === "transcribing") return `transcribing${suffix}`;
    return "converting…";
  }

  function formatHardwareLine(hw) {
    if (!hw) return "";
    const parts = [hw.chip_or_gpu || hw.os];
    if (hw.os === "macos" && hw.ram_mb > 0) {
      parts.push(`${Math.round(hw.ram_mb / 1024)} GB Unified Memory`);
      parts.push("Apple Neural Engine");
    } else {
      if (hw.vram_mb >= 1024) parts.push(`${(hw.vram_mb / 1024).toFixed(0)} GB VRAM`);
      if (hw.vulkan) parts.push("Vulkan GPU");
      else parts.push("CPU mode");
    }
    return parts.filter(Boolean).join(" · ");
  }

  async function probeMediaDurations(paths) {
    if (!paths.length) return new Map();
    try {
      const out = await Command.sidecar("binaries/docprep-audio", ["--probe", ...paths]).execute();
      if (out.code === 0 && out.stdout.trim()) {
        const parsed = JSON.parse(out.stdout.trim());
        const map = new Map();
        for (const entry of parsed) {
          map.set(entry.path, entry);
        }
        return map;
      }
    } catch (e) {
      console.warn("[docprep-audio probe]", e);
    }
    return new Map();
  }

  async function refreshModelCatalog() {
    if (catalogLoading) return modelCatalog;
    catalogLoading = true;
    try {
      const out = await Command.sidecar("binaries/docprep-core", ["--list-models", "--json"]).execute();
      if (out.code === 0 && out.stdout.trim()) {
        const line = out.stdout
          .trim()
          .split("\n")
          .find((l) => l.trim().startsWith("{"));
        if (line) {
          modelCatalog = JSON.parse(line);
          if (!selectedSetupModel && modelCatalog.recommended_model) {
            selectedSetupModel = modelCatalog.recommended_model;
          }
          if (!s.audioModel && modelCatalog.active_model) {
            s.audioModel = modelCatalog.active_model;
          }
        }
      }
    } catch (e) {
      console.warn("[list-models]", e);
    } finally {
      catalogLoading = false;
    }
    return modelCatalog;
  }

  async function scanFolderSupported(dir) {
    let count = 0;
    const mediaPaths = [];
    const queue = [dir];
    while (queue.length) {
      const current = queue.pop();
      let entries;
      try {
        entries = await readDir(current);
      } catch {
        continue;
      }
      for (const e of entries) {
        if (e.name.startsWith("~$") || e.name.startsWith(".")) continue;
        const full = joinPath(current, e.name);
        if (e.isDirectory) {
          queue.push(full);
        } else {
          const ext = extOf(e.name);
          if (SUPPORTED.has(ext)) {
            count++;
            if (MEDIA_EXTS.has(ext)) mediaPaths.push(full);
          }
        }
      }
    }
    mediaPaths.sort();
    return { count, mediaPaths };
  }

  async function addPaths(paths) {
    if (state === "done") reset();
    const existing = new Set(items.map((i) => i.path));
    const directMediaToProbe = [];

    for (const p of paths) {
      if (existing.has(p)) continue;
      existing.add(p);
      let isDir = false;
      try {
        isDir = (await stat(p)).isDirectory;
      } catch {
        continue;
      }
      const name = baseName(p);
      const isMedia = !isDir && isMediaName(name);
      items.push({
        path: p,
        name,
        isDir,
        isMedia,
        included: true,
        durationSec: null,
        hasAudio: true,
        liveStatus: "",
        skipRequested: false,
        count: isDir ? null : 1,
        done: 0,
        skipped: 0,
        tokens: 0,
        errors: 0,
        src: 0,
        outCmp: 0,
        stagedMedia: [],
        children: [],
      });
      const tracked = items[items.length - 1];
      if (isMedia) {
        directMediaToProbe.push(tracked);
      }
      if (isDir) {
        scanFolderSupported(p)
          .then(async ({ count, mediaPaths }) => {
            tracked.count = count;
            tracked.stagedMedia = mediaPaths.map((mp) => ({
              path: mp,
              name: baseName(mp),
              included: true,
              durationSec: null,
              hasAudio: true,
            }));
            if (mediaPaths.length) {
              if (!modelCatalog) refreshModelCatalog();
              const probed = await probeMediaDurations(mediaPaths);
              for (const m of tracked.stagedMedia) {
                const info = probed.get(m.path);
                if (info) {
                  m.durationSec = info.duration_sec;
                  m.hasAudio = info.has_audio;
                }
              }
            }
          })
          .catch(() => {
            tracked.count = 0;
          });
      }
    }

    if (items.length) state = "staged";

    if (directMediaToProbe.length) {
      if (!modelCatalog) refreshModelCatalog();
      const probed = await probeMediaDurations(directMediaToProbe.map((t) => t.path));
      for (const t of directMediaToProbe) {
        const info = probed.get(t.path);
        if (info) {
          t.durationSec = info.duration_sec;
          t.hasAudio = info.has_audio;
        }
      }
    }
  }

  async function browseFiles() {
    const sel = await open({
      multiple: true,
      filters: [
        {
          name: "Documents, Audio & Video",
          extensions: [...SUPPORTED, ...(s.zip ? ["zip"] : [])],
        },
      ],
    });
    if (sel) addPaths(Array.isArray(sel) ? sel : [sel]);
  }
  async function browseFolder() {
    const sel = await open({ directory: true });
    if (sel) addPaths([sel]);
  }

  function removeItem(item) {
    if (state === "running") return;
    items = items.filter((i) => i !== item);
    if (!items.length) state = "empty";
  }

  function reset() {
    items = [];
    summary = null;
    progress = { done: 0, total: 0, current: "" };
    footerMsg = "";
    state = "empty";
  }

  function ownerOf(filePath) {
    return items.find(
      (i) => filePath === i.path || filePath.startsWith(i.path + "\\") || filePath.startsWith(i.path + "/")
    );
  }

  function openSettings() {
    showSettings = true;
    refreshModelCatalog();
  }

  function openSetupDialog(autoStartAfter = false) {
    pendingAutoConvert = autoStartAfter;
    if (modelCatalog?.recommended_model && !selectedSetupModel) {
      selectedSetupModel = modelCatalog.recommended_model;
    }
    if (setupDialogEl && !setupDialogEl.open) {
      setupDialogEl.showModal();
    }
  }

  function closeSetupDialog() {
    if (activeDownload) return;
    pendingAutoConvert = false;
    if (setupDialogEl?.open) {
      setupDialogEl.close();
    }
  }

  function handleSetupDialogClose() {
    if (setupDialogEl) {
      setupDialogEl.returnValue = "";
    }
  }

  function handleSetupPointerDown(e) {
    if (!setupDialogEl || activeDownload) return;
    if ("closedBy" in HTMLDialogElement.prototype) return;
    const rect = setupDialogEl.getBoundingClientRect();
    pointerDownOutsideSetup =
      e.clientX < rect.left ||
      e.clientX > rect.right ||
      e.clientY < rect.top ||
      e.clientY > rect.bottom;
  }

  function handleSetupPointerUp(e) {
    if (!setupDialogEl || activeDownload || !pointerDownOutsideSetup) return;
    if ("closedBy" in HTMLDialogElement.prototype) return;
    const rect = setupDialogEl.getBoundingClientRect();
    const upOutside =
      e.clientX < rect.left ||
      e.clientX > rect.right ||
      e.clientY < rect.top ||
      e.clientY > rect.bottom;
    pointerDownOutsideSetup = false;
    if (upOutside) closeSetupDialog();
  }

  async function startModelDownload(modelId, autoConvertOnFinish = false) {
    if (!modelId || activeDownload) return;
    activeDownload = { modelId, pct: 0, stage: "starting", error: null };

    const cmd = Command.sidecar("binaries/docprep-core", [
      "--download-model",
      modelId,
      "--json",
      "--watchdog",
    ]);

    let finishedOk = false;
    let errText = "";

    cmd.stdout.on("data", (line) => {
      let ev;
      try {
        ev = JSON.parse(line);
      } catch {
        return;
      }
      if (ev.event === "download") {
        activeDownload = {
          modelId,
          pct: ev.pct ?? 0,
          stage: ev.stage || "asr",
          error: null,
        };
      } else if (ev.event === "download_complete") {
        finishedOk = true;
        activeDownload = { modelId, pct: 100, stage: "complete", error: null };
      } else if (ev.event === "error") {
        errText = ev.message || "Download failed";
      }
    });

    cmd.stderr.on("data", (line) => {
      const t = line.trim();
      if (t) errText = t;
    });

    const onDone = async () => {
      downloadChild = null;
      await refreshModelCatalog();
      const nowInstalled = (modelCatalog?.models || []).some((m) => m.id === modelId && m.installed);
      if (finishedOk || nowInstalled) {
        s.audioModel = modelId;
        activeDownload = null;
        if (setupDialogEl?.open) {
          setupDialogEl.close();
        }
        if (autoConvertOnFinish || pendingAutoConvert) {
          pendingAutoConvert = false;
          runConversion();
        }
      } else {
        activeDownload = {
          modelId,
          pct: 0,
          stage: "error",
          error: errText || "Download cancelled or interrupted",
        };
      }
    };

    cmd.on("close", onDone);
    cmd.on("error", onDone);
    downloadChild = await cmd.spawn();
  }

  async function cancelModelDownload() {
    if (!downloadChild) {
      activeDownload = null;
      return;
    }
    try {
      await downloadChild.kill();
    } catch (e) {
      console.warn("[cancelModelDownload]", e);
    }
    activeDownload = null;
  }

  async function removeInstalledModel(modelId) {
    if (activeDownload) return;
    try {
      await Command.sidecar("binaries/docprep-core", ["--delete-model", modelId, "--json"]).execute();
      if (s.audioModel === modelId) s.audioModel = "";
      await refreshModelCatalog();
    } catch (e) {
      footerMsg = `Could not delete model: ${e}`;
    }
  }

  function buildArgs() {
    const activePaths = items.filter((i) => i.isDir || i.included !== false).map((i) => i.path);
    const args = [...activePaths, "--json", "--watchdog", "--output", s.outputMode, "--conflict", s.conflict];
    if (!s.yaml) args.push("--no-yaml");
    if (!s.toc) args.push("--no-toc");
    if (s.zip) args.push("--allow-zip");
    if (s.ocr) args.push("--ocr");
    if (!s.diarize) args.push("--no-diarize");
    else if (s.speakers && s.speakers !== "auto") args.push("--speakers", s.speakers);
    if (s.audioModel) args.push("--audio-model", s.audioModel);

    for (const item of items) {
      if (item.isDir && item.stagedMedia?.length) {
        for (const m of item.stagedMedia) {
          if (!m.included) args.push("--exclude", m.path);
        }
      }
    }

    if (s.redact) {
      args.push("--redact", "--engine", s.engine);
      if (s.engine === "ollama") args.push("--ollama-model", s.ollamaModel);
      for (const t of s.customTerms.split("\n")) {
        if (t.trim()) args.push("--term", t.trim());
      }
    }
    return args;
  }

  // rerun: repeat the last batch with OCR on, replacing its outputs in place
  async function convert(rerun = false) {
    if (!items.length || state === "running" || totalFiles === 0) return;
    if (rerun && !(lastArgs && summary?.manifest)) return;

    if (!rerun && hasIncludedMedia) {
      const cat = modelCatalog || (await refreshModelCatalog());
      const anyInstalled = (cat?.models || []).some((m) => m.installed);
      if (!anyInstalled) {
        selectedSetupModel = cat?.recommended_model || cat?.models?.[0]?.id || "";
        openSetupDialog(true);
        return;
      }
    }

    await runConversion(rerun);
  }

  async function skipMediaFile(filePath, ownerItem = null, childRow = null) {
    if (!child) return;
    if (ownerItem && !ownerItem.isDir) {
      ownerItem.skipRequested = true;
      ownerItem.liveStatus = "skipping…";
    }
    if (childRow) {
      childRow.skipRequested = true;
      childRow.liveStatus = "skipping…";
    }
    try {
      await child.write(JSON.stringify({ cmd: "skip", file: filePath }) + "\n");
    } catch (e) {
      console.error("[skip] write failed", e);
    }
  }

  async function runConversion(rerun = false) {
    if (!items.length || state === "running") return;
    if (rerun && !(lastArgs && summary?.manifest)) return;
    const args = rerun ? [...lastArgs, "--ocr", "--replace-from", summary.manifest] : (lastArgs = buildArgs());
    state = "running";
    summary = null;
    footerMsg = "";
    for (const i of items) {
      i.done = 0;
      i.skipped = 0;
      i.tokens = 0;
      i.errors = 0;
      i.src = 0;
      i.outCmp = 0;
      i.unread = 0;
      i.liveStatus = "";
      i.skipRequested = false;
      i.children = [];
    }

    progress = { done: 0, total: totalFiles, current: rerun ? "reading images…" : "starting up…" };
    let stderrTail = "";

    const cmd = Command.sidecar("binaries/docprep-core", manifestPath ? [...args, "--manifest", manifestPath] : args);
    cmd.stdout.on("data", (line) => {
      let ev;
      try {
        ev = JSON.parse(line);
      } catch {
        return;
      }
      if (ev.event === "start") {
        progress = { done: 0, total: ev.total, current: "converting…" };
      } else if (ev.event === "file" && ev.status === "started") {
        const name = baseName(ev.file);
        const media = isMediaName(name);
        const label = media ? "preparing audio…" : "converting…";
        progress = { ...progress, total: ev.total, current: `${name} (${label})` };
        const it = ownerOf(ev.file);
        if (it) {
          if (it.isDir) {
            it.children.push({
              path: ev.file,
              name,
              isMedia: media,
              status: "converting",
              liveStatus: label,
              skipRequested: false,
              tokens: 0,
              durationSec: null,
              error: null,
            });
          } else {
            it.liveStatus = label;
          }
        }
      } else if (ev.event === "file" && ev.status === "progress") {
        const name = baseName(ev.file);
        const stageText = formatStageLabel(ev.stage, ev.pct);
        progress = { ...progress, current: `${name} (${stageText})` };
        const it = ownerOf(ev.file);
        if (it) {
          if (it.isDir) {
            const c = it.children.find((x) => x.path === ev.file);
            if (c && !c.skipRequested) c.liveStatus = stageText;
          } else if (!it.skipRequested) {
            it.liveStatus = stageText;
          }
        }
      } else if (ev.event === "file") {
        progress = { done: ev.done, total: ev.total, current: baseName(ev.file) };
        const it = ownerOf(ev.file);
        if (it) {
          it.done += 1;
          it.liveStatus = "";
          if (ev.status === "error") {
            it.errors += 1;
          } else if (ev.status === "skipped") {
            it.skipped = (it.skipped || 0) + 1;
          } else {
            it.tokens += ev.tokens;
            if (ev.duration_sec) it.durationSec = ev.duration_sec;
            if (ev.source_tokens) {
              it.src += ev.source_tokens;
              it.outCmp += ev.tokens;
            }
            // Images/scanned pages OCR would have read but didn't (OCR off)
            it.unread = (it.unread || 0) + (ev.unread_images || 0);
          }
          if (it.isDir) {
            const c = it.children.find((x) => x.path === ev.file);
            if (c) {
              c.status = ev.status === "error" ? "error" : ev.status === "skipped" ? "skipped" : "done";
              c.liveStatus = "";
              c.tokens = ev.tokens;
              c.durationSec = ev.duration_sec || c.durationSec;
              c.error = ev.error;
              c.unread = ev.unread_images || 0;
            }
          } else if (ev.status === "error") {
            it.lastError = ev.error;
          }
        }
      } else if (ev.event === "summary") {
        summary = ev;
        revealTarget = ev.combined || (ev.outputs?.length ? ev.outputs[0] : "");
      }
    });
    cmd.stderr.on("data", (line) => {
      const trimmed = line.trim();
      if (trimmed && !trimmed.includes("RuntimeWarning") && !trimmed.startsWith("warn(")) stderrTail = trimmed;
      console.warn("[docprep-core]", line);
    });
    const finish = () => {
      if (!summary) {
        // Process died or was cancelled before its summary arrived
        summary = {
          cancelled: true,
          converted: items.reduce((n, i) => n + Math.max(0, i.done - i.errors - (i.skipped || 0)), 0),
          failed: items.reduce((n, i) => n + i.errors, 0),
          skipped: items.reduce((n, i) => n + (i.skipped || 0), 0),
          tokens: 0,
          elapsed: 0,
          outputs: [],
        };
      }
      if ((summary.failed > 0 || summary.cancelled) && stderrTail) footerMsg = stderrTail;
      state = "done";
      child = null;
      // A re-run writes to the files already shown, so don't open the folder again
      if (s.revealWhenDone && revealTarget && !summary.cancelled && !rerun) showInFolder();
    };
    cmd.on("close", finish);
    cmd.on("error", finish);
    child = await cmd.spawn();
  }

  async function cancel() {
    if (!child) return;
    footerMsg = "Cancelling…";
    try {
      // Needs shell:allow-kill in capabilities/default.json; without it this
      // rejects and, unawaited, used to fail silently while conversion went on.
      await child.kill();
    } catch (e) {
      console.error("[cancel] kill failed", e);
      footerMsg = "Cancel failed: " + (e?.message ?? e);
    }
  }

  async function showInFolder() {
    try {
      footerMsg = "";
      await revealItemInDir(revealTarget);
    } catch (e) {
      footerMsg = `Could not open folder: ${e}`;
    }
  }

  async function copyMarkdown() {
    try {
      let text = "";
      if (summary?.combined) {
        text = await readTextFile(summary.combined);
      } else {
        const parts = [];
        for (const p of summary?.outputs ?? []) {
          parts.push(`\n\n---\n## ${baseName(p)}\n\n` + (await readTextFile(p)));
        }
        text = parts.join("").trimStart();
      }
      if (!text) {
        footerMsg = "Nothing to copy";
        return;
      }
      await writeText(text);
      footerMsg = `Copied ~${Math.max(1, Math.floor(text.length / 4)).toLocaleString()} tokens to clipboard`;
    } catch (e) {
      footerMsg = `Copy failed: ${e}`;
    }
  }
</script>

<main>
  <header>
    <span class="brand">
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
        <rect x="1" y="1" width="20" height="20" rx="3" stroke="#17160F" stroke-width="1.6" />
        <path d="M5.5 6.5h11" stroke="#17160F" stroke-width="1.6" stroke-linecap="round" />
        <rect x="5.5" y="9.6" width="11" height="3" rx="1" fill="#17160F" />
        <path d="M5.5 15.5h7" stroke="#17160F" stroke-width="1.6" stroke-linecap="round" />
      </svg>
      AI DocPrep
      {#if appVersion}<span class="tag">{appVersion}</span>{/if}
    </span>
    <button class="gear" aria-label="Settings" onclick={openSettings}>&#9881;</button>
  </header>

  {#if state === "empty"}
    <section class="dropzone" role="button" tabindex="0" onclick={browseFiles} onkeydown={(e) => e.key === "Enter" && browseFiles()}>
      <div class="arrow mono">&#8595;</div>
      <h2>Drop files or folders here</h2>
      <p class="sub">Documents, audio &amp; video converted to clean, speaker-labeled Markdown</p>
      <div class="btns">
        <button class="primary" onclick={(e) => { e.stopPropagation(); browseFiles(); }}>Choose Files…</button>
        <button onclick={(e) => { e.stopPropagation(); browseFolder(); }}>Choose Folder…</button>
      </div>
      <div class="chips">
        {#each ["PDF", "DOCX", "PPTX", "XLSX", "EPUB", "HTML", "VTT", "MP3", "M4A", "WAV", "MP4", "MOV"] as f}<span class="chip mono">{f}</span>{/each}
      </div>
    </section>
  {:else}
    {#if state === "done" && summary}
      <div class="banner" class:warn={summary.failed > 0 || summary.cancelled}>
        {#if summary.cancelled}
          <span>Stopped — <span class="mono">{summary.converted}</span> files converted</span>
        {:else}
          <span>&#10003; <span class="mono">{summary.converted}</span> files converted in <span class="mono">{summary.elapsed.toFixed(1)}s</span> · <span class="mono">~{summary.tokens.toLocaleString()}</span> tokens</span>
          {#if totalPct}<span class="pill mono">&#8722;{totalPct}% vs raw</span>{/if}
          {#if summary.skipped > 0}<span class="meta mono">{summary.skipped} skipped</span>{/if}
          {#if summary.failed > 0}<span class="fail mono">{summary.failed} failed</span>{/if}
        {/if}
      </div>
      {#if !summary.cancelled && summary.manifest && unreadFiles > 0}
        <div class="ocrnote">
          <span><span class="mono">{unreadFiles}</span> {unreadFiles === 1 ? "file has" : "files have"} text inside images that wasn't read.</span>
          <button class="small primary" onclick={() => convert(true)}>Read with OCR</button>
        </div>
      {/if}
    {/if}
    <div class="listhead mono">
      <span>{items.length} item{items.length === 1 ? "" : "s"} · {scanning ? "scanning…" : `${totalFiles} file${totalFiles === 1 ? "" : "s"}`}</span>
      {#if state === "staged"}
        <span class="headbtns">
          <button class="small" onclick={browseFiles}>+ Files</button>
          <button class="small" onclick={browseFolder}>+ Folder</button>
          <button class="small" onclick={reset}>Clear</button>
        </span>
      {/if}
    </div>
    <ul class="queue">
      {#each items as item}
        <li class:excluded={state === "staged" && !item.isDir && item.included === false}>
          {#if state === "staged" && item.isMedia}
            <input
              type="checkbox"
              aria-label={`Include ${item.name}`}
              bind:checked={item.included}
            />
          {/if}
          <span class="badge mono">{item.isDir ? "FOLDER" : extOf(item.name).toUpperCase()}</span>
          <span class="name">{item.name}</span>
          {#if item.isDir}
            <span class="meta mono">{item.count === null ? "scanning…" : `${includedFolderCount(item)} files`}</span>
          {:else if item.isMedia}
            {#if !item.hasAudio}
              <span class="fail mono">no audio track</span>
            {:else if item.durationSec}
              <span class="dur mono">{formatDuration(item.durationSec)}</span>
            {/if}
          {/if}

          {#if state === "running" && !item.isDir && item.liveStatus}
            <span class="meta mono">{item.liveStatus}</span>
            {#if item.isMedia && !item.skipRequested}
              <button class="small skipbtn" onclick={() => skipMediaFile(item.path, item, null)}>Skip</button>
            {/if}
          {/if}

          {#if item.skipped && !item.isDir}
            <span class="meta mono">skipped</span>
          {:else if item.tokens}
            <span class="status mono">&#10003; ~{item.tokens.toLocaleString()} tokens</span>
            {#if rowPct(item)}<span class="pill mono">&#8722;{rowPct(item)}%</span>{/if}
          {/if}
          {#if item.unread}<span class="unread mono" title="Has text inside images or scanned pages that wasn't read. Click “Read with OCR” above, or turn on “Read text in images and scanned PDFs” in Settings.">{item.unread} img unread</span>{/if}
          {#if item.errors}<span class="fail mono" title={item.lastError || ""}>{item.errors} failed</span>{/if}
          {#if state === "staged"}<button class="x" aria-label={`Remove ${item.name}`} onclick={() => removeItem(item)}>&#10005;</button>{/if}
        </li>

        {#if state === "staged" && item.isDir && item.stagedMedia?.length}
          {#each item.stagedMedia as m}
            <li class="child" class:excluded={!m.included}>
              <input
                type="checkbox"
                aria-label={`Transcribe ${m.name}`}
                bind:checked={m.included}
              />
              <span class="childname">{m.name}</span>
              {#if !m.hasAudio}
                <span class="fail mono">no audio</span>
              {:else if m.durationSec}
                <span class="dur mono">{formatDuration(m.durationSec)}</span>
              {/if}
              <span class="meta mono">{m.included ? "transcribe" : "skip"}</span>
            </li>
          {/each}
        {/if}

        {#each item.children as c}
          <li class="child">
            <span class="childname">{c.name}</span>
            {#if c.durationSec && c.status !== "converting"}
              <span class="dur mono">{formatDuration(c.durationSec)}</span>
            {/if}
            {#if c.status === "converting"}
              <span class="meta mono">{c.liveStatus || "converting…"}</span>
              {#if c.isMedia && !c.skipRequested}
                <button class="small skipbtn" onclick={() => skipMediaFile(c.path, item, c)}>Skip</button>
              {/if}
            {:else if c.status === "skipped"}
              <span class="meta mono">skipped</span>
            {:else if c.status === "error"}
              <span class="fail mono" title={c.error}>{(c.error || "failed").slice(0, 48)}</span>
            {:else}
              {#if c.unread}<span class="unread mono" title="Has text inside images or scanned pages that wasn't read. Click “Read with OCR” above, or turn on “Read text in images and scanned PDFs” in Settings.">{c.unread} img unread</span>{/if}
              <span class="status mono">~{c.tokens.toLocaleString()} tok</span>
            {/if}
          </li>
        {/each}
      {/each}
    </ul>

    {#if state === "staged"}
      <div class="options">
        <label>
          <span class="mono optlabel">Output</span>
          <select bind:value={s.outputMode} disabled={totalFiles < 2}>
            {#each OUTPUT_MODES as m}<option value={m.value}>{m.label}</option>{/each}
          </select>
        </label>
        <div class="toggles">
          {#if hasIncludedMedia}
            <label class="redact" class:active={s.diarize}>
              <input type="checkbox" bind:checked={s.diarize} /> Identify speakers
            </label>
          {/if}
          <label class="redact" class:active={s.redact}><input type="checkbox" bind:checked={s.redact} /> Redact PII</label>
        </div>
      </div>
      <button class="primary big" onclick={() => convert()} disabled={scanning || totalFiles === 0}>
        {scanning ? "Scanning folders…" : totalFiles === 0 ? "No supported files selected" : `Convert ${totalFiles} file${totalFiles === 1 ? "" : "s"}`}
      </button>
    {:else if state === "running"}
      <progress max={progress.total || 1} value={progress.done}></progress>
      <p class="footer mono">Converting {progress.done} of {progress.total} — {progress.current}</p>
      <button class="danger big" onclick={cancel}>Cancel</button>
    {:else if state === "done"}
      <div class="actions">
        <span>
          {#if revealTarget}<button onclick={showInFolder}>Show in Folder</button>{/if}
          <button onclick={copyMarkdown}>Copy Markdown</button>
        </span>
        <button class="primary" onclick={reset}>New Conversion</button>
      </div>
    {/if}
  {/if}
  <p class="footer offline mono">{footerMsg || "100% offline — files never leave this machine"}</p>

  <!-- Native modal dialog for first-run offline speech model setup -->
  <dialog
    bind:this={setupDialogEl}
    class="setup-dialog"
    aria-labelledby="setup-audio-title"
    closedby={activeDownload ? "none" : "any"}
    onclose={handleSetupDialogClose}
    onpointerdown={handleSetupPointerDown}
    onpointerup={handleSetupPointerUp}
  >
    <div class="sheethead">
      <h3 id="setup-audio-title">Set up offline transcription</h3>
      {#if !activeDownload}
        <button class="x" aria-label="Close" onclick={closeSetupDialog}>&#10005;</button>
      {/if}
    </div>
    <p class="dialog-lead">
      Audio &amp; video files are transcribed 100% on your device with speaker identification. Download a one-time speech model to begin:
    </p>

    {#if modelCatalog?.hardware}
      <div class="hwbox mono">
        Detected: {formatHardwareLine(modelCatalog.hardware)}
      </div>
    {/if}

    <div class="modellist">
      {#each modelCatalog?.models || [] as m}
        <label class="modelcard" class:selected={selectedSetupModel === m.id}>
          <input
            type="radio"
            name="setup-model"
            value={m.id}
            bind:group={selectedSetupModel}
            disabled={Boolean(activeDownload)}
          />
          <div class="modelbody">
            <div class="modeltitle">
              <strong>{m.name}</strong>
              {#if m.recommended}<span class="pill mono">Recommended</span>{/if}
              <span class="meta mono">~{m.size_mb} MB</span>
            </div>
            <div class="hint mono">{m.languages} · {m.description}</div>
          </div>
        </label>
      {/each}
    </div>

    {#if activeDownload}
      <progress max="100" value={activeDownload.pct}></progress>
      <div class="dlrow mono">
        <span>
          {activeDownload.stage === "diarizer"
            ? `Downloading speaker diarizer… ${activeDownload.pct}%`
            : `Downloading speech model… ${activeDownload.pct}%`}
        </span>
        <button class="small danger" onclick={cancelModelDownload}>Cancel</button>
      </div>
      {#if activeDownload.error}
        <p class="fail mono">{activeDownload.error}</p>
      {/if}
    {:else}
      <div class="dialog-actions">
        <button onclick={closeSetupDialog}>Not now</button>
        <button
          class="primary"
          disabled={!selectedSetupModel}
          onclick={() => startModelDownload(selectedSetupModel, pendingAutoConvert)}
        >
          Download &amp; Transcribe
        </button>
      </div>
    {/if}
  </dialog>

  {#if showSettings}
    <div class="overlay" role="button" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget && !activeDownload) showSettings = false; }} onkeydown={(e) => e.key === "Escape" && !activeDownload && (showSettings = false)}>
      <div class="sheet">
        <div class="sheethead">
          <h3>Settings</h3>
          <button class="small primary" disabled={Boolean(activeDownload)} onclick={() => (showSettings = false)}>Done</button>
        </div>

        <p class="group tag">Output</p>
        <label class="row">
          If a file already exists
          <select bind:value={s.conflict}>
            <option value="keep-both">Keep both (adds “ (1)”)</option>
            <option value="overwrite">Overwrite</option>
          </select>
        </label>
        <label class="row"><input type="checkbox" bind:checked={s.revealWhenDone} /> Show output in folder when done</label>
        <label class="row">
          <input type="checkbox" bind:checked={s.zip} /> Convert ZIP archives
          <span class="infowrap" tabindex="0">
            <span class="infoicon mono">i</span>
            <span class="infopop">Archives can contain anything, so this is off by default. When on: contents are filtered to supported document formats (audio/video inside archives are skipped), capped at 200 entries / 200 MB, and nested archives are skipped.</span>
          </span>
        </label>

        <label class="row">
          <input type="checkbox" bind:checked={s.ocr} /> Read text in images and scanned PDFs
          <span class="infowrap" tabindex="0">
            <span class="infoicon mono">i</span>
            <span class="infopop">Runs on-device OCR so screenshots in slides and scanned PDFs stop being invisible. Off by default because it's far slower — seconds per image, and a picture-heavy deck can take minutes. PDFs that already contain text are untouched. Image files convert when dropped directly; folder scans still skip them.</span>
          </span>
        </label>

        <p class="group tag">Audio &amp; Video</p>
        {#if modelCatalog?.hardware}
          <div class="hwbox mono">{formatHardwareLine(modelCatalog.hardware)}</div>
        {/if}
        <label class="row">
          <input type="checkbox" bind:checked={s.diarize} /> Identify speakers (Speaker 1, Speaker 2…)
          <span class="infowrap" tabindex="0">
            <span class="infoicon mono">i</span>
            <span class="infopop">Clusters voices locally so meetings, interviews, and lectures are grouped into timestamped speaker paragraphs. No voiceprints are stored after the file finishes.</span>
          </span>
        </label>
        {#if s.diarize}
          <label class="row indent">
            Number of speakers
            <select bind:value={s.speakers}>
              {#each SPEAKER_OPTIONS as opt}<option value={opt.value}>{opt.label}</option>{/each}
            </select>
          </label>
        {/if}

        <div class="modellist compact">
          {#if catalogLoading && !modelCatalog}
            <p class="hint mono">Checking offline speech models…</p>
          {:else if modelCatalog?.models?.length}
            {#each modelCatalog.models as m}
              <div class="modelrow">
                <div class="modelbody">
                  <div class="modeltitle">
                    {#if m.installed && installedModels.length > 1}
                      <input type="radio" name="active-audio-model" value={m.id} bind:group={s.audioModel} />
                    {/if}
                    <strong>{m.name}</strong>
                    {#if m.recommended}<span class="pill mono">Recommended</span>{/if}
                    <span class="meta mono">
                      {m.installed && m.disk_bytes > 0
                        ? `${Math.round(m.disk_bytes / (1024 * 1024))} MB on disk`
                        : `~${m.size_mb} MB`}
                    </span>
                  </div>
                  <div class="hint mono">{m.languages}</div>
                </div>
                <div class="modelbtns">
                  {#if activeDownload?.modelId === m.id}
                    <span class="meta mono">{activeDownload.pct}%</span>
                    <button class="small danger" onclick={cancelModelDownload}>Cancel</button>
                  {:else if m.installed}
                    <button class="small" disabled={Boolean(activeDownload)} onclick={() => removeInstalledModel(m.id)}>Delete</button>
                  {:else}
                    <button class="small primary" disabled={Boolean(activeDownload)} onclick={() => startModelDownload(m.id, false)}>Download</button>
                  {/if}
                </div>
              </div>
            {/each}
          {/if}
          {#if activeDownload?.error}
            <p class="fail mono">{activeDownload.error}</p>
          {/if}
        </div>

        <p class="group tag">Markdown</p>
        <label class="row"><input type="checkbox" bind:checked={s.yaml} /> Add YAML frontmatter (Obsidian / Notion)</label>
        <label class="row"><input type="checkbox" bind:checked={s.toc} /> Table of contents in combined files</label>

        <p class="group tag">Redaction engine</p>
        <label class="row"><input type="radio" bind:group={s.engine} value="regex" /> Pattern matching <span class="hint mono">instant · emails, SSNs, cards, keys, IPs</span></label>
        <label class="row"><input type="radio" bind:group={s.engine} value="ner" /> On-device AI <span class="hint mono">also catches names, orgs, locations · offline</span></label>
        <label class="row"><input type="radio" bind:group={s.engine} value="ollama" /> Local LLM <span class="hint mono">deepest · needs Ollama running</span></label>
        {#if s.engine === "ollama"}
          <label class="row indent">
            Model
            <input type="text" bind:value={s.ollamaModel} style="width: 140px" />
          </label>
        {/if}
        <label class="col">
          Custom terms to always redact (one per line)
          <textarea rows="4" bind:value={s.customTerms}></textarea>
        </label>
      </div>
    </div>
  {/if}
</main>

<style>
  :root {
    --paper: #fbfbf8;
    --panel: #f2f1ec;
    --ink: #17160f;
    --mut: #6c6a5f;
    --line: #e4e3da;
    --line-strong: #c9c8bc;
    --hl: #ffe23b;
    --font-display: "Avenir Next", "Avenir", "Segoe UI Variable Display", "Segoe UI", -apple-system, system-ui, sans-serif;
    --font-body: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, Roboto, sans-serif;
    --font-mono: "SF Mono", ui-monospace, "Cascadia Code", Consolas, "Liberation Mono", monospace;
  }
  :global(body) {
    margin: 0;
    font-family: var(--font-body);
    background: var(--paper);
    color: var(--ink);
    border-top: 5px solid var(--ink);
  }
  :global(::selection) { background: var(--hl); color: var(--ink); }
  .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  main { max-width: 720px; margin: 0 auto; padding: 14px 20px; display: flex; flex-direction: column; min-height: 95vh; box-sizing: border-box; }
  header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; }
  .brand { display: flex; align-items: center; gap: 9px; font-family: var(--font-display); font-weight: 800; font-size: 19px; letter-spacing: -0.02em; }
  .brand svg { display: block; }
  .tag { font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--mut); margin-left: 4px; }
  .gear { width: 34px; height: 34px; padding: 0; font-size: 15px; border-radius: 3px; }
  .dropzone {
    flex: 1;
    background: var(--paper);
    border: 1.5px dashed var(--line-strong);
    border-radius: 4px;
    box-shadow: 8px 8px 0 var(--line);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 40px 20px;
    margin-bottom: 10px;
  }
  .arrow { font-size: 46px; line-height: 1; color: var(--ink); }
  h2 { font-family: var(--font-display); font-weight: 700; letter-spacing: -0.022em; font-size: 19px; margin: 10px 0 3px; }
  h3 { font-family: var(--font-display); font-weight: 700; font-size: 16px; margin: 0; }
  .sub { color: var(--mut); font-size: 13px; margin: 0 0 18px; text-align: center; }
  .btns { display: flex; gap: 10px; }
  button {
    font-family: var(--font-body);
    font-size: 13px;
    padding: 8px 16px;
    border-radius: 3px;
    border: 1.5px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    cursor: pointer;
  }
  button:hover:not(:disabled) { background: var(--panel); }
  button:disabled { opacity: 0.45; cursor: default; border-color: var(--line-strong); }
  button.primary { background: var(--ink); color: var(--paper); }
  button.primary:hover:not(:disabled) { background: #2c2a20; }
  button.danger { background: var(--paper); border-color: #b3261e; color: #b3261e; }
  button.big { width: 100%; padding: 13px; font-size: 15px; font-weight: 600; margin-top: 10px; box-shadow: 4px 4px 0 var(--panel); }
  button.small, button.x { padding: 4px 10px; font-size: 11px; }
  button.x { background: none; border: none; color: var(--mut); }
  button.skipbtn { padding: 2px 8px; font-size: 11px; }
  .chips { display: flex; flex-wrap: wrap; justify-content: center; gap: 7px; margin-top: 20px; max-width: 460px; }
  .chip { font-size: 10px; letter-spacing: 0.1em; border: 1px solid var(--line-strong); color: var(--mut); border-radius: 2px; padding: 3px 8px; background: var(--paper); }
  .pill { background: var(--hl); color: var(--ink); border: 1px solid var(--ink); border-radius: 2px; padding: 1px 7px; font-size: 10px; font-weight: 600; white-space: nowrap; }
  .dur { background: var(--panel); color: var(--ink); border: 1px solid var(--line-strong); border-radius: 2px; padding: 1px 6px; font-size: 11px; white-space: nowrap; }
  .fail { color: #b3261e; font-size: 12px; }
  .banner {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    background: var(--paper);
    border: 1px solid var(--line-strong);
    box-shadow: 6px 6px 0 var(--line);
    color: var(--ink);
    font-weight: 600;
    font-size: 13px;
    border-radius: 3px;
    padding: 12px 14px;
    margin-bottom: 12px;
  }
  .banner.warn { border-color: #b3261e; }
  .ocrnote { display: flex; align-items: center; justify-content: space-between; gap: 10px; background: var(--panel); border: 1px solid var(--line-strong); border-radius: 3px; padding: 8px 10px 8px 14px; margin: -4px 0 12px; font-size: 12px; }
  .listhead { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--mut); margin-bottom: 6px; letter-spacing: 0.04em; }
  .headbtns { display: flex; gap: 6px; }
  .queue {
    flex: 1;
    list-style: none;
    margin: 0 0 10px;
    padding: 2px 0;
    background: var(--paper);
    border: 1px solid var(--line-strong);
    border-radius: 4px;
    box-shadow: 6px 6px 0 var(--line);
    overflow-y: auto;
  }
  .queue li { display: flex; align-items: center; gap: 10px; padding: 9px 12px; font-size: 13px; border-bottom: 1px solid var(--line); }
  .queue li:last-child { border-bottom: none; }
  .queue li.excluded { opacity: 0.52; }
  .queue li.child { padding: 5px 12px 5px 34px; font-size: 12px; border-bottom: none; color: var(--mut); position: relative; }
  .queue li.child::before { content: ""; position: absolute; left: 22px; top: 0; bottom: 0; width: 1px; background: var(--line); }
  .childname { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .badge { font-size: 9px; letter-spacing: 0.08em; border: 1px solid var(--line-strong); color: var(--mut); border-radius: 2px; padding: 2px 6px; min-width: 42px; text-align: center; }
  .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta { color: var(--mut); font-size: 12px; white-space: nowrap; }
  .status { color: var(--ink); font-size: 12px; white-space: nowrap; }
  .options { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 2px; font-size: 13px; }
  .toggles { display: flex; align-items: center; gap: 6px; }
  .optlabel { font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--mut); margin-right: 6px; }
  .redact { display: flex; align-items: center; gap: 7px; border: 1px solid transparent; border-radius: 2px; padding: 4px 10px; cursor: pointer; }
  .redact.active { background: var(--hl); border-color: var(--ink); font-weight: 600; }
  select, input[type="text"], textarea { font-family: var(--font-body); font-size: 13px; padding: 5px 8px; border-radius: 2px; border: 1px solid var(--line-strong); background: var(--paper); color: var(--ink); }
  select {
    appearance: none;
    -webkit-appearance: none;
    font-family: var(--font-mono);
    font-size: 12px;
    border: 1.5px solid var(--ink);
    padding: 6px 28px 6px 10px;
    background: var(--paper) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%2317160F' stroke-width='1.5' fill='none'/%3E%3C/svg%3E") no-repeat right 9px center;
  }
  select:disabled { border-color: var(--line-strong); color: var(--mut); }
  input[type="checkbox"], input[type="radio"] { accent-color: var(--ink); }
  .actions { display: flex; gap: 8px; margin-top: 10px; justify-content: space-between; }
  .actions span { display: flex; gap: 8px; }
  progress { width: 100%; margin-top: 12px; accent-color: var(--ink); }
  .footer { font-size: 11px; color: var(--mut); margin: 8px 0 0; }
  .overlay { position: fixed; inset: 0; background: rgba(23, 22, 15, 0.4); display: flex; align-items: center; justify-content: center; z-index: 20; }
  .sheet { background: var(--paper); border: 1.5px solid var(--ink); box-shadow: 8px 8px 0 var(--panel); border-radius: 4px; padding: 18px 20px; width: min(500px, 92vw); max-height: 86vh; overflow-y: auto; }
  .sheethead { display: flex; justify-content: space-between; align-items: center; }
  .group { display: inline-flex; align-items: center; gap: 8px; font-size: 10px; font-weight: 600; margin: 18px 0 6px; }
  .group::before { content: ""; width: 8px; height: 8px; background: var(--hl); border: 1px solid var(--ink); display: inline-block; }
  .row { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 5px 0; justify-content: flex-start; }
  .row select { margin-left: auto; }
  .row.indent { padding-left: 24px; }
  .hint { color: var(--mut); font-size: 10px; letter-spacing: 0.02em; line-height: 1.4; }
  .infowrap { position: relative; display: inline-flex; cursor: help; outline: none; }
  .infoicon { width: 15px; height: 15px; border: 1px solid var(--line-strong); border-radius: 50%; color: var(--mut); font-size: 10px; display: inline-flex; align-items: center; justify-content: center; }
  .infopop { display: none; position: absolute; left: 22px; top: -6px; z-index: 5; width: 260px; background: var(--paper); border: 1.5px solid var(--ink); box-shadow: 4px 4px 0 var(--line); border-radius: 3px; padding: 9px 11px; font-size: 11px; font-weight: 400; line-height: 1.45; color: var(--ink); }
  .infowrap:hover .infopop, .infowrap:focus .infopop { display: block; }
  .unread { color: var(--mut); font-size: 11px; margin-right: 8px; cursor: help; border-bottom: 1px dotted var(--line-strong); }
  .col { display: flex; flex-direction: column; gap: 6px; font-size: 13px; padding: 8px 0; }

  /* Native <dialog> setup modal (modern-web-guidance compliant) */
  dialog.setup-dialog {
    background: var(--paper);
    color: var(--ink);
    border: 1.5px solid var(--ink);
    box-shadow: 8px 8px 0 var(--panel);
    border-radius: 4px;
    padding: 18px 20px;
    width: min(490px, 92vw);
    max-height: 85vh;
    overflow-y: auto;
  }
  dialog.setup-dialog[open] {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  dialog.setup-dialog:modal {
    overscroll-behavior: contain;
  }
  dialog.setup-dialog::backdrop {
    background: rgba(23, 22, 15, 0.45);
    overflow: hidden;
    overscroll-behavior: contain;
  }
  .dialog-lead { margin: 2px 0; font-size: 13px; color: var(--mut); line-height: 1.45; }
  .hwbox { background: var(--panel); border: 1px solid var(--line-strong); border-radius: 3px; padding: 6px 10px; font-size: 11px; color: var(--ink); margin: 4px 0; }
  .modellist { display: flex; flex-direction: column; gap: 8px; margin-top: 4px; }
  .modellist.compact { gap: 6px; margin-top: 6px; }
  .modelcard {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--line-strong);
    border-radius: 3px;
    background: var(--paper);
    cursor: pointer;
  }
  .modelcard.selected { border: 1.5px solid var(--ink); background: var(--panel); }
  .modelrow {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 8px 10px;
    border: 1px solid var(--line-strong);
    border-radius: 3px;
    background: var(--paper);
  }
  .modelbody { display: flex; flex-direction: column; gap: 3px; flex: 1; min-width: 0; }
  .modeltitle { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; font-size: 13px; }
  .modelbtns { display: flex; align-items: center; gap: 6px; }
  .dlrow { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--mut); margin-top: 4px; }
  .dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
</style>
