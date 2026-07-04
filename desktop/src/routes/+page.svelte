<script>
  import { onMount } from "svelte";
  import { getCurrentWebview } from "@tauri-apps/api/webview";
  import { open } from "@tauri-apps/plugin-dialog";
  import { Command } from "@tauri-apps/plugin-shell";
  import { revealItemInDir } from "@tauri-apps/plugin-opener";

  const OUTPUT_MODES = [
    { value: "individual", label: "Individual .md files" },
    { value: "both", label: "Individual + combined file" },
    { value: "combined-only", label: "Combined file only" },
  ];

  let state = $state("empty"); // empty | staged | running | done
  let items = $state([]); // { path, name, isDir, done, tokens, errors }
  let outputMode = $state(localStorage.getItem("outputMode") || "both");
  let redact = $state(localStorage.getItem("redact") === "1");
  let progress = $state({ done: 0, total: 0, current: "" });
  let summary = $state(null);
  let revealTarget = $state("");
  let footerMsg = $state("");
  let child = null;

  $effect(() => {
    localStorage.setItem("outputMode", outputMode);
    localStorage.setItem("redact", redact ? "1" : "0");
  });

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
  function dirName(p) {
    const sep = p.includes("\\") ? "\\" : "/";
    const parts = p.replace(/[\\/]+$/, "").split(/[\\/]/);
    parts.pop();
    return parts.join(sep);
  }

  function addPaths(paths) {
    if (state === "done") reset();
    const existing = new Set(items.map((i) => i.path));
    for (const p of paths) {
      if (existing.has(p)) continue;
      existing.add(p);
      const isDir = !/\.[a-z0-9]{2,5}$/i.test(p);
      items.push({ path: p, name: baseName(p), isDir, done: 0, tokens: 0, errors: 0 });
    }
    if (items.length) state = "staged";
  }

  async function browseFiles() {
    const sel = await open({
      multiple: true,
      filters: [{ name: "Documents", extensions: ["docx", "pdf", "pptx", "xlsx", "html", "htm", "vtt"] }],
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
    state = "empty";
  }

  function ownerOf(filePath) {
    return items.find(
      (i) => filePath === i.path || filePath.startsWith(i.path + "\\") || filePath.startsWith(i.path + "/")
    );
  }

  async function convert() {
    if (!items.length || state === "running") return;
    state = "running";
    summary = null;
    for (const i of items) {
      i.done = 0;
      i.tokens = 0;
      i.errors = 0;
    }

    const args = [...items.map((i) => i.path), "--json", "--output", outputMode];
    if (redact) args.push("--redact");

    const cmd = Command.sidecar("binaries/docprep-core", args);
    cmd.stdout.on("data", (line) => {
      let ev;
      try {
        ev = JSON.parse(line);
      } catch {
        return;
      }
      if (ev.event === "file") {
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
    cmd.on("close", () => {
      state = "done";
      child = null;
    });
    cmd.on("error", () => {
      state = "done";
      child = null;
    });
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
</script>

<main>
  <header>
    <h1>AI DocPrep <span class="ver">2.0 alpha</span></h1>
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
      <span>{items.length} item{items.length === 1 ? "" : "s"}</span>
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
          <span class="badge">{item.isDir ? "FOLDER" : item.name.split(".").pop().toUpperCase()}</span>
          <span class="name">{item.name}</span>
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
          <select bind:value={outputMode}>
            {#each OUTPUT_MODES as m}<option value={m.value}>{m.label}</option>{/each}
          </select>
        </label>
        <label><input type="checkbox" bind:checked={redact} /> Redact PII</label>
      </div>
      <button class="primary big" onclick={convert}>Convert</button>
    {:else if state === "running"}
      <progress max={progress.total || 1} value={progress.done}></progress>
      <p class="footer">Converting {progress.done} of {progress.total} — {progress.current}</p>
      <button class="danger big" onclick={cancel}>Cancel</button>
    {:else if state === "done"}
      <div class="actions">
        {#if revealTarget}<button onclick={showInFolder}>Show in Folder</button>{/if}
        <button class="primary" onclick={reset}>New Conversion</button>
      </div>
    {/if}
  {/if}
  <p class="footer offline">{footerMsg || "100% offline — files never leave this machine"}</p>
</main>

<style>
  :global(body) {
    margin: 0;
    font-family: "Segoe UI", -apple-system, sans-serif;
    background: #f2f2f7;
    color: #1c1c1e;
  }
  main {
    max-width: 720px;
    margin: 0 auto;
    padding: 16px 20px;
    display: flex;
    flex-direction: column;
    min-height: 96vh;
    box-sizing: border-box;
  }
  h1 { font-size: 22px; margin: 0 0 12px; }
  .ver { font-size: 11px; color: #8e8e93; font-weight: 400; }
  .dropzone {
    flex: 1;
    background: #fff;
    border: 1.5px dashed #c7c7cc;
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 40px 20px;
  }
  .arrow { font-size: 36px; color: #aeaeb2; }
  h2 { font-size: 17px; margin: 8px 0 2px; }
  .sub { color: #8e8e93; font-size: 13px; margin: 0 0 16px; }
  .btns { display: flex; gap: 8px; }
  button {
    font: inherit;
    font-size: 13px;
    padding: 8px 16px;
    border-radius: 8px;
    border: 1px solid #d1d1d6;
    background: #e5e5ea;
    cursor: pointer;
  }
  button.primary { background: #007aff; border-color: #007aff; color: #fff; }
  button.danger { background: #d9534f; border-color: #d9534f; color: #fff; }
  button.big { width: 100%; padding: 13px; font-size: 15px; font-weight: 600; margin-top: 10px; }
  button.small, button.x { padding: 4px 10px; font-size: 12px; }
  button.x { background: none; border: none; color: #8e8e93; }
  .chips { display: flex; gap: 6px; margin-top: 18px; }
  .chip { font-size: 10px; background: #e5e5ea; color: #636366; border-radius: 6px; padding: 3px 8px; }
  .banner {
    background: #dff2e1;
    color: #1b7a3d;
    font-weight: 600;
    font-size: 13px;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 10px;
  }
  .banner.warn { background: #fdf0db; color: #92400e; }
  .listhead { display: flex; justify-content: space-between; align-items: center; font-size: 12px; color: #8e8e93; margin-bottom: 6px; }
  .headbtns { display: flex; gap: 6px; }
  .queue {
    flex: 1;
    list-style: none;
    margin: 0;
    padding: 4px;
    background: #fff;
    border: 1px solid #e5e5ea;
    border-radius: 10px;
    overflow-y: auto;
  }
  .queue li { display: flex; align-items: center; gap: 10px; padding: 8px 10px; font-size: 13px; border-bottom: 1px solid #f2f2f7; }
  .queue li:last-child { border-bottom: none; }
  .badge {
    font-size: 9px;
    font-weight: 700;
    background: #e5e5ea;
    color: #636366;
    border-radius: 5px;
    padding: 3px 7px;
    min-width: 40px;
    text-align: center;
  }
  .name { flex: 1; }
  .status { color: #1b7a3d; font-size: 12px; }
  .err { color: #d9534f; }
  .options { display: flex; justify-content: space-between; align-items: center; margin-top: 10px; font-size: 13px; color: #636366; }
  .options select { font: inherit; padding: 5px 8px; border-radius: 7px; border: 1px solid #d1d1d6; }
  .actions { display: flex; gap: 8px; margin-top: 10px; justify-content: space-between; }
  progress { width: 100%; margin-top: 12px; }
  .footer { font-size: 11px; color: #8e8e93; margin: 8px 0 0; }
</style>
