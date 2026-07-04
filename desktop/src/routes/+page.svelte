<script>
  import { onMount } from "svelte";
  import { getCurrentWebview } from "@tauri-apps/api/webview";
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
  const SUPPORTED = new Set(["docx", "pdf", "pptx", "xlsx", "html", "htm", "vtt"]);

  const DEFAULT_SETTINGS = {
    outputMode: "both",
    redact: false,
    conflict: "keep-both",
    yaml: true,
    toc: true,
    engine: "regex", // "regex" | "ner" | "ollama"
    ollamaModel: "llama3",
    customTerms: "",
    revealWhenDone: true,
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
  let items = $state([]); // { path, name, isDir, count, done, tokens, errors }
  let progress = $state({ done: 0, total: 0, current: "" });
  let summary = $state(null);
  let revealTarget = $state("");
  let footerMsg = $state("");
  let showSettings = $state(false);
  let child = null;

  $effect(() => {
    localStorage.setItem("docprep.settings", JSON.stringify(s));
  });

  const totalFiles = $derived(items.reduce((n, i) => n + (i.isDir ? i.count ?? 0 : 1), 0));
  const scanning = $derived(items.some((i) => i.isDir && i.count === null));

  const totalPct = $derived.by(() => {
    if (!summary || !(summary.src_tokens > summary.out_comparable) || !(summary.out_comparable > 0)) return null;
    const pct = Math.min(99, Math.round(100 * (1 - summary.out_comparable / summary.src_tokens)));
    return pct > 0 ? pct : null;
  });

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
  function joinPath(dir, name) {
    const sep = dir.includes("\\") ? "\\" : "/";
    return dir.replace(/[\\/]+$/, "") + sep + name;
  }

  async function countSupported(dir) {
    let count = 0;
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
        if (e.isDirectory) queue.push(joinPath(current, e.name));
        else if (SUPPORTED.has(extOf(e.name))) count++;
      }
    }
    return count;
  }

  async function addPaths(paths) {
    if (state === "done") reset();
    const existing = new Set(items.map((i) => i.path));
    for (const p of paths) {
      if (existing.has(p)) continue;
      existing.add(p);
      let isDir = false;
      try {
        isDir = (await stat(p)).isDirectory;
      } catch {
        continue;
      }
      const item = { path: p, name: baseName(p), isDir, count: isDir ? null : 1, done: 0, tokens: 0, errors: 0 };
      items.push(item);
      if (isDir) countSupported(p).then((n) => (item.count = n));
    }
    if (items.length) state = "staged";
  }

  async function browseFiles() {
    const sel = await open({
      multiple: true,
      filters: [{ name: "Documents", extensions: [...SUPPORTED] }],
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

  function buildArgs() {
    const args = [...items.map((i) => i.path), "--json", "--watchdog", "--output", s.outputMode, "--conflict", s.conflict];
    if (!s.yaml) args.push("--no-yaml");
    if (!s.toc) args.push("--no-toc");
    if (s.redact) {
      args.push("--redact", "--engine", s.engine);
      if (s.engine === "ollama") args.push("--ollama-model", s.ollamaModel);
      for (const t of s.customTerms.split("\n")) {
        if (t.trim()) args.push("--term", t.trim());
      }
    }
    return args;
  }

  async function convert() {
    if (!items.length || state === "running") return;
    state = "running";
    summary = null;
    footerMsg = "";
    for (const i of items) {
      i.done = 0;
      i.tokens = 0;
      i.errors = 0;
    }

    progress = { done: 0, total: totalFiles, current: "starting up…" };
    let stderrTail = "";

    const cmd = Command.sidecar("binaries/docprep-core", buildArgs());
    cmd.stdout.on("data", (line) => {
      let ev;
      try {
        ev = JSON.parse(line);
      } catch {
        return;
      }
      if (ev.event === "start") {
        progress = { done: 0, total: ev.total, current: "converting…" };
      } else if (ev.event === "file") {
        progress = { done: ev.done, total: ev.total, current: baseName(ev.file) };
        const it = ownerOf(ev.file);
        if (it) {
          it.done += 1;
          if (ev.status === "error") it.errors += 1;
          else it.tokens += ev.tokens;
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
          converted: items.reduce((n, i) => n + i.done - i.errors, 0),
          failed: items.reduce((n, i) => n + i.errors, 0),
          tokens: 0,
          elapsed: 0,
          outputs: [],
        };
      }
      if ((summary.failed > 0 || summary.cancelled) && stderrTail) footerMsg = stderrTail;
      state = "done";
      child = null;
      if (s.revealWhenDone && revealTarget && !summary.cancelled) showInFolder();
    };
    cmd.on("close", finish);
    cmd.on("error", finish);
    child = await cmd.spawn();
  }

  function cancel() {
    child?.kill();
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
    <h1>AI DocPrep <span class="ver">2.0 alpha</span></h1>
    <button class="gear" aria-label="Settings" onclick={() => (showSettings = true)}>&#9881;</button>
  </header>

  {#if state === "empty"}
    <section class="dropzone" role="button" tabindex="0" onclick={browseFiles} onkeydown={(e) => e.key === "Enter" && browseFiles()}>
      <div class="arrow">&#8595;</div>
      <h2>Drop files or folders here</h2>
      <p class="sub">Converted to clean, token-efficient Markdown</p>
      <div class="btns">
        <button class="primary" onclick={(e) => { e.stopPropagation(); browseFiles(); }}>Choose Files…</button>
        <button onclick={(e) => { e.stopPropagation(); browseFolder(); }}>Choose Folder…</button>
      </div>
      <div class="chips">
        {#each ["PDF", "DOCX", "PPTX", "XLSX", "HTML", "VTT"] as f}<span class="chip">{f}</span>{/each}
      </div>
    </section>
  {:else}
    {#if state === "done" && summary}
      <div class="banner" class:warn={summary.failed > 0 || summary.cancelled}>
        {#if summary.cancelled}
          Stopped — {summary.converted} files converted
        {:else}
          &#10003; {summary.converted} files converted in {summary.elapsed}s · ~{summary.tokens.toLocaleString()} tokens{#if totalPct}
            · {totalPct}% saved vs raw{/if}
        {/if}
      </div>
    {/if}
    <div class="listhead">
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
        <li>
          <span class="badge">{item.isDir ? "FOLDER" : extOf(item.name).toUpperCase()}</span>
          <span class="name">{item.name}</span>
          <span class="meta">{item.isDir ? (item.count === null ? "scanning…" : `${item.count} files`) : ""}</span>
          <span class="status">
            {#if item.tokens}&#10003; ~{item.tokens.toLocaleString()} tokens{/if}
            {#if item.errors}<span class="err">{item.errors} failed</span>{/if}
          </span>
          {#if state === "staged"}<button class="x" onclick={() => removeItem(item)}>&#10005;</button>{/if}
        </li>
      {/each}
    </ul>

    {#if state === "staged"}
      <div class="options">
        <label>
          Output
          <select bind:value={s.outputMode} disabled={totalFiles < 2}>
            {#each OUTPUT_MODES as m}<option value={m.value}>{m.label}</option>{/each}
          </select>
        </label>
        <label><input type="checkbox" bind:checked={s.redact} /> Redact PII</label>
      </div>
      <button class="primary big" onclick={convert} disabled={scanning || totalFiles === 0}>
        {scanning ? "Scanning folders…" : totalFiles === 0 ? "No supported files found" : `Convert ${totalFiles} file${totalFiles === 1 ? "" : "s"}`}
      </button>
    {:else if state === "running"}
      <progress max={progress.total || 1} value={progress.done}></progress>
      <p class="footer">Converting {progress.done} of {progress.total} — {progress.current}</p>
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
  <p class="footer offline">{footerMsg || "100% offline — files never leave this machine"}</p>

  {#if showSettings}
    <div class="overlay" role="button" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showSettings = false; }} onkeydown={(e) => e.key === "Escape" && (showSettings = false)}>
      <div class="sheet">
        <div class="sheethead">
          <h3>Settings</h3>
          <button class="small primary" onclick={() => (showSettings = false)}>Done</button>
        </div>

        <p class="group">Output</p>
        <label class="row">
          If a file already exists
          <select bind:value={s.conflict}>
            <option value="keep-both">Keep both (adds “ (1)”)</option>
            <option value="overwrite">Overwrite</option>
          </select>
        </label>
        <label class="row"><input type="checkbox" bind:checked={s.revealWhenDone} /> Show output in folder when done</label>

        <p class="group">Markdown</p>
        <label class="row"><input type="checkbox" bind:checked={s.yaml} /> Add YAML frontmatter (Obsidian / Notion)</label>
        <label class="row"><input type="checkbox" bind:checked={s.toc} /> Table of contents in combined files</label>

        <p class="group">Redaction engine</p>
        <label class="row"><input type="radio" bind:group={s.engine} value="regex" /> Pattern matching <span class="hint">instant · emails, SSNs, cards, keys, IPs</span></label>
        <label class="row"><input type="radio" bind:group={s.engine} value="ner" /> On-device AI <span class="hint">also catches names, orgs, locations · offline</span></label>
        <label class="row"><input type="radio" bind:group={s.engine} value="ollama" /> Local LLM <span class="hint">deepest · needs Ollama running</span></label>
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
    --bg: #f2f2f7;
    --card: #ffffff;
    --text: #1c1c1e;
    --muted: #8e8e93;
    --border: #e5e5ea;
    --border2: #d1d1d6;
    --chipbg: #e5e5ea;
    --chiptext: #636366;
    --ok: #1b7a3d;
    --okbg: #dff2e1;
    --warn: #92400e;
    --warnbg: #fdf0db;
    --accent: #007aff;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1c1e;
      --card: #2c2c2e;
      --text: #f2f2f7;
      --muted: #98989d;
      --border: #3a3a3c;
      --border2: #48484a;
      --chipbg: #3a3a3c;
      --chiptext: #aeaeb2;
      --ok: #4cc38a;
      --okbg: #173b23;
      --warn: #f5c544;
      --warnbg: #3b2e14;
      --accent: #0a84ff;
    }
  }
  :global(body) { margin: 0; font-family: "Segoe UI", -apple-system, sans-serif; background: var(--bg); color: var(--text); }
  main { max-width: 720px; margin: 0 auto; padding: 16px 20px; display: flex; flex-direction: column; min-height: 96vh; box-sizing: border-box; }
  header { display: flex; justify-content: space-between; align-items: center; }
  h1 { font-size: 22px; margin: 0 0 12px; }
  .ver { font-size: 11px; color: var(--muted); font-weight: 400; }
  .gear { border-radius: 50%; width: 34px; height: 34px; padding: 0; font-size: 16px; }
  .dropzone { flex: 1; background: var(--card); border: 1.5px dashed var(--border2); border-radius: 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; cursor: pointer; padding: 40px 20px; }
  .arrow { font-size: 36px; color: var(--muted); }
  h2 { font-size: 17px; margin: 8px 0 2px; }
  .sub { color: var(--muted); font-size: 13px; margin: 0 0 16px; }
  .btns { display: flex; gap: 8px; }
  button { font: inherit; font-size: 13px; padding: 8px 16px; border-radius: 8px; border: 1px solid var(--border2); background: var(--chipbg); color: var(--text); cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.danger { background: #d9534f; border-color: #d9534f; color: #fff; }
  button.big { width: 100%; padding: 13px; font-size: 15px; font-weight: 600; margin-top: 10px; }
  button.small, button.x { padding: 4px 10px; font-size: 12px; }
  button.x { background: none; border: none; color: var(--muted); }
  .chips { display: flex; gap: 6px; margin-top: 18px; }
  .chip { font-size: 10px; background: var(--chipbg); color: var(--chiptext); border-radius: 6px; padding: 3px 8px; }
  .banner { background: var(--okbg); color: var(--ok); font-weight: 600; font-size: 13px; border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; }
  .banner.warn { background: var(--warnbg); color: var(--warn); }
  .listhead { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .headbtns { display: flex; gap: 6px; }
  .queue { flex: 1; list-style: none; margin: 0; padding: 4px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow-y: auto; }
  .queue li { display: flex; align-items: center; gap: 10px; padding: 8px 10px; font-size: 13px; border-bottom: 1px solid var(--bg); }
  .queue li:last-child { border-bottom: none; }
  .badge { font-size: 9px; font-weight: 700; background: var(--chipbg); color: var(--chiptext); border-radius: 5px; padding: 3px 7px; min-width: 40px; text-align: center; }
  .name { flex: 1; }
  .meta { color: var(--muted); font-size: 12px; }
  .status { color: var(--ok); font-size: 12px; }
  .err { color: #d9534f; }
  .options { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; font-size: 13px; color: var(--chiptext); }
  select, input[type="text"], textarea { font: inherit; font-size: 13px; padding: 5px 8px; border-radius: 7px; border: 1px solid var(--border2); background: var(--card); color: var(--text); }
  .actions { display: flex; gap: 8px; margin-top: 10px; justify-content: space-between; }
  .actions span { display: flex; gap: 8px; }
  progress { width: 100%; margin-top: 12px; }
  .footer { font-size: 11px; color: var(--muted); margin: 8px 0 0; }
  .overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.35); display: flex; align-items: center; justify-content: center; }
  .sheet { background: var(--card); border-radius: 14px; padding: 18px 20px; width: min(460px, 90vw); max-height: 84vh; overflow-y: auto; }
  .sheethead { display: flex; justify-content: space-between; align-items: center; }
  .sheet h3 { margin: 0; font-size: 16px; }
  .group { font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin: 18px 0 6px; }
  .row { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 5px 0; justify-content: flex-start; }
  .row select { margin-left: auto; }
  .row.indent { padding-left: 24px; }
  .hint { color: var(--muted); font-size: 11px; }
  .col { display: flex; flex-direction: column; gap: 6px; font-size: 13px; padding: 8px 0; }
</style>
