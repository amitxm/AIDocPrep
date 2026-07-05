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
  let items = $state([]); // { path, name, isDir, count, done, tokens, errors, src, outCmp }
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
      items.push({ path: p, name: baseName(p), isDir, count: isDir ? null : 1, done: 0, tokens: 0, errors: 0, src: 0, outCmp: 0 });
      if (isDir) {
        // Mutations must go through the $state proxy in the array — writing
        // to the raw pre-push object is invisible to Svelte's reactivity
        const tracked = items[items.length - 1];
        countSupported(p)
          .then((n) => (tracked.count = n))
          .catch(() => (tracked.count = 0));
      }
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
      i.src = 0;
      i.outCmp = 0;
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
      } else if (ev.event === "file" && ev.status === "started") {
        progress = { ...progress, total: ev.total, current: baseName(ev.file) + "…" };
      } else if (ev.event === "file") {
        progress = { done: ev.done, total: ev.total, current: baseName(ev.file) };
        const it = ownerOf(ev.file);
        if (it) {
          it.done += 1;
          if (ev.status === "error") it.errors += 1;
          else {
            it.tokens += ev.tokens;
            if (ev.source_tokens) {
              it.src += ev.source_tokens;
              it.outCmp += ev.tokens;
            }
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
    <span class="brand">
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden="true">
        <rect x="1" y="1" width="20" height="20" rx="3" stroke="#17160F" stroke-width="1.6" />
        <path d="M5.5 6.5h11" stroke="#17160F" stroke-width="1.6" stroke-linecap="round" />
        <rect x="5.5" y="9.6" width="11" height="3" rx="1" fill="#17160F" />
        <path d="M5.5 15.5h7" stroke="#17160F" stroke-width="1.6" stroke-linecap="round" />
      </svg>
      AI DocPrep
      <span class="tag">v2.0 alpha</span>
    </span>
    <button class="gear" aria-label="Settings" onclick={() => (showSettings = true)}>&#9881;</button>
  </header>

  {#if state === "empty"}
    <section class="dropzone" role="button" tabindex="0" onclick={browseFiles} onkeydown={(e) => e.key === "Enter" && browseFiles()}>
      <div class="arrow mono">&#8595;</div>
      <h2>Drop files or folders here</h2>
      <p class="sub">Converted to clean, token-efficient Markdown</p>
      <div class="btns">
        <button class="primary" onclick={(e) => { e.stopPropagation(); browseFiles(); }}>Choose Files…</button>
        <button onclick={(e) => { e.stopPropagation(); browseFolder(); }}>Choose Folder…</button>
      </div>
      <div class="chips">
        {#each ["PDF", "DOCX", "PPTX", "XLSX", "HTML", "VTT"] as f}<span class="chip mono">{f}</span>{/each}
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
          {#if summary.failed > 0}<span class="fail mono">{summary.failed} failed</span>{/if}
        {/if}
      </div>
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
        <li>
          <span class="badge mono">{item.isDir ? "FOLDER" : extOf(item.name).toUpperCase()}</span>
          <span class="name">{item.name}</span>
          <span class="meta mono">{item.isDir ? (item.count === null ? "scanning…" : `${item.count} files`) : ""}</span>
          {#if item.tokens}
            <span class="status mono">&#10003; ~{item.tokens.toLocaleString()} tokens</span>
            {#if rowPct(item)}<span class="pill mono">&#8722;{rowPct(item)}%</span>{/if}
          {/if}
          {#if item.errors}<span class="fail mono">{item.errors} failed</span>{/if}
          {#if state === "staged"}<button class="x" onclick={() => removeItem(item)}>&#10005;</button>{/if}
        </li>
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
        <label class="redact" class:active={s.redact}><input type="checkbox" bind:checked={s.redact} /> Redact PII</label>
      </div>
      <button class="primary big" onclick={convert} disabled={scanning || totalFiles === 0}>
        {scanning ? "Scanning folders…" : totalFiles === 0 ? "No supported files found" : `Convert ${totalFiles} file${totalFiles === 1 ? "" : "s"}`}
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

  {#if showSettings}
    <div class="overlay" role="button" tabindex="-1" onclick={(e) => { if (e.target === e.currentTarget) showSettings = false; }} onkeydown={(e) => e.key === "Escape" && (showSettings = false)}>
      <div class="sheet">
        <div class="sheethead">
          <h3>Settings</h3>
          <button class="small primary" onclick={() => (showSettings = false)}>Done</button>
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
    box-shadow: 8px 8px 0 var(--panel);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 40px 20px;
    margin-bottom: 10px;
  }
  .arrow { font-size: 34px; color: var(--mut); }
  h2 { font-family: var(--font-display); font-weight: 700; letter-spacing: -0.022em; font-size: 19px; margin: 10px 0 3px; }
  h3 { font-family: var(--font-display); font-weight: 700; font-size: 16px; margin: 0; }
  .sub { color: var(--mut); font-size: 13px; margin: 0 0 18px; }
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
  .chips { display: flex; gap: 7px; margin-top: 20px; }
  .chip { font-size: 10px; letter-spacing: 0.1em; border: 1px solid var(--line-strong); color: var(--mut); border-radius: 2px; padding: 3px 8px; background: var(--paper); }
  .pill { background: var(--hl); color: var(--ink); border: 1px solid var(--ink); border-radius: 2px; padding: 1px 8px; font-size: 12px; font-weight: 600; white-space: nowrap; }
  .fail { color: #b3261e; font-size: 12px; }
  .banner {
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--paper);
    border: 1px solid var(--line-strong);
    box-shadow: 4px 4px 0 var(--panel);
    color: var(--ink);
    font-weight: 600;
    font-size: 13px;
    border-radius: 3px;
    padding: 12px 14px;
    margin-bottom: 12px;
  }
  .banner.warn { border-color: #b3261e; }
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
    box-shadow: 6px 6px 0 var(--panel);
    overflow-y: auto;
  }
  .queue li { display: flex; align-items: center; gap: 10px; padding: 9px 12px; font-size: 13px; border-bottom: 1px solid var(--line); }
  .queue li:last-child { border-bottom: none; }
  .badge { font-size: 9px; letter-spacing: 0.08em; border: 1px solid var(--line-strong); color: var(--mut); border-radius: 2px; padding: 2px 6px; min-width: 42px; text-align: center; }
  .name { flex: 1; }
  .meta { color: var(--mut); font-size: 12px; }
  .status { color: var(--ink); font-size: 12px; }
  .options { display: flex; justify-content: space-between; align-items: center; margin-top: 2px; font-size: 13px; }
  .optlabel { font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; color: var(--mut); margin-right: 6px; }
  .redact { display: flex; align-items: center; gap: 7px; border: 1px solid transparent; border-radius: 2px; padding: 4px 10px; }
  .redact.active { background: var(--hl); border-color: var(--ink); font-weight: 600; }
  select, input[type="text"], textarea { font-family: var(--font-body); font-size: 13px; padding: 5px 8px; border-radius: 2px; border: 1px solid var(--line-strong); background: var(--paper); color: var(--ink); }
  input[type="checkbox"], input[type="radio"] { accent-color: var(--ink); }
  .actions { display: flex; gap: 8px; margin-top: 10px; justify-content: space-between; }
  .actions span { display: flex; gap: 8px; }
  progress { width: 100%; margin-top: 12px; accent-color: var(--ink); }
  .footer { font-size: 11px; color: var(--mut); margin: 8px 0 0; }
  .overlay { position: fixed; inset: 0; background: rgba(23, 22, 15, 0.4); display: flex; align-items: center; justify-content: center; }
  .sheet { background: var(--paper); border: 1.5px solid var(--ink); box-shadow: 8px 8px 0 var(--panel); border-radius: 4px; padding: 18px 20px; width: min(460px, 90vw); max-height: 84vh; overflow-y: auto; }
  .sheethead { display: flex; justify-content: space-between; align-items: center; }
  .group { display: inline-flex; align-items: center; gap: 8px; font-size: 10px; font-weight: 600; margin: 18px 0 6px; }
  .group::before { content: ""; width: 8px; height: 8px; background: var(--hl); border: 1px solid var(--ink); display: inline-block; }
  .row { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 5px 0; justify-content: flex-start; }
  .row select { margin-left: auto; }
  .row.indent { padding-left: 24px; }
  .hint { color: var(--mut); font-size: 10px; letter-spacing: 0.02em; }
  .col { display: flex; flex-direction: column; gap: 6px; font-size: 13px; padding: 8px 0; }
</style>
