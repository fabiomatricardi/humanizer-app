// Humanizer local web app – client script
// Relies on window.APP_CONFIG set by env.js and on /api/* endpoints.

(() => {
  "use strict";

  // ------------------------------------------------------- config & api
  const apiBase = (window.APP_CONFIG && window.APP_CONFIG.apiBase) ||
    "http://127.0.0.1:8774"; // fallback; overridden by env.js
  const form = document.getElementById("config-form");
  const cfgBase = document.getElementById("cfg-base-url");
  const cfgKey = document.getElementById("cfg-api-key");
  const cfgModel = document.getElementById("cfg-model");
  const cfgTempRange = document.getElementById("cfg-temp-range");
  const cfgTempNum = document.getElementById("cfg-temp");
  const cfgMaxTokens = document.getElementById("cfg-max-tokens");
  const cfgSample = document.getElementById("cfg-sample");
  const cfgNoReason = document.getElementById("cfg-no-reason");
  const cfgIntensity = document.getElementById("cfg-intensity");
  const cfgVerifySsl = document.getElementById("cfg-verify-ssl");
  const cfgMultiPass = document.getElementById("cfg-multi-pass");
  const srcPane = document.querySelector(".pane.left");
  const phaseNote = document.getElementById("phase-note");
  const boardMsg = document.getElementById("board-msg");
  const boardTimer = document.getElementById("board-timer");
  const testResult = document.getElementById("test-result");
  const statusDot = document.querySelector("#status-dot .dot");
  const statusText = document.getElementById("status-text");

  function $(s) { return document.querySelector(s); }

  function setStatus(ok, msg) {
    statusDot.classList.toggle("ok", ok === true);
    statusDot.classList.toggle("err", ok === false);
    statusText.textContent = msg || (ok ? "OK" : "error");
  }

  // fetch helper with JSON body
  async function apiFetch(path, opts = {}) {
    const url = new URL(path, apiBase).href;
    const resp = await fetch(url, {
      method: (opts.method || "GET"),
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (!resp.ok) {
      const err = await resp.text().catch(() => resp.statusText);
      throw new Error(`HTTP ${resp.status}: ${err}`);
    }
    return resp.json();
  }

  // ------------------------------------------------------- tab switch
  const tabHumanizer = $("#tab-humanizer");
  const tabConfig = $("#tab-config");
  const viewHumanizer = $("#view-humanizer");
  const viewConfig = $("#view-config");

  function showTab(show, hide) {
    hide.hidden = true;
    show.hidden = false;
    tabHumanizer.classList.toggle("active", show === viewHumanizer);
    tabConfig.classList.toggle("active", show === viewConfig);
  }
  tabHumanizer.addEventListener("click", () => showTab(viewHumanizer, viewConfig));
  tabConfig.addEventListener("click", () => showTab(viewConfig, viewHumanizer));

  // ------------------------------------------------------- config form
  window.addEventListener("load", async () => {
    try {
      const resp = await apiFetch("/api/config");
      if (!resp.ok) throw new Error("config fetch failed");
      const c = resp.config;
      cfgBase.value = c.base_url || "";
      cfgModel.value = c.model_name || "";
      cfgKey.value = c.api_key || "";
      cfgTempNum.value = c.temperature ?? 0.7;
      cfgTempRange.value = c.temperature ?? 0.7;
      cfgMaxTokens.value = c.max_tokens ?? 4000;
      cfgSample.value = c.writing_sample || "";
      cfgNoReason.checked = Boolean(c.disable_reasoning);
      cfgIntensity.value = c.edit_intensity === "light" ? "light" : "thorough";
      cfgVerifySsl.checked = c.verify_ssl !== false;
      cfgMultiPass.checked = c.multi_pass !== false;
      setStatus(resp.configured, resp.configured ? "Configured" : "Not configured");
    } catch (e) {
      setStatus(false, "Backend unreachable");
      console.error(e);
    }
  });

  // API key show/hide
  const eyeToggle = document.getElementById("eye-toggle");
  eyeToggle.addEventListener("click", () => {
    const hidden = cfgKey.type === "password";
    cfgKey.type = hidden ? "text" : "password";
    eyeToggle.textContent = hidden ? "🙈" : "👁";
    eyeToggle.setAttribute("aria-label", hidden ? "Hide API key" : "Show API key");
  });

  // temperature slider <-> number sync
  cfgTempRange.addEventListener("input", () => { cfgTempNum.value = cfgTempRange.value; });
  cfgTempNum.addEventListener("input", () => {
    if (cfgTempNum.value !== "") cfgTempRange.value = cfgTempNum.value;
  });

  // save config
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const newCfg = {
      base_url: cfgBase.value.trim() || "",
      api_key: cfgKey.value.trim(),
      model_name: cfgModel.value.trim() || "",
      temperature: cfgTempNum.value === "" ? null : parseFloat(cfgTempNum.value),
      max_tokens: cfgMaxTokens.value === "" ? null : parseInt(cfgMaxTokens.value, 10),
      writing_sample: cfgSample.value.trim(),
      disable_reasoning: cfgNoReason.checked,
      edit_intensity: cfgIntensity.value === "light" ? "light" : "thorough",
      verify_ssl: cfgVerifySsl.checked,
      multi_pass: cfgMultiPass.checked,
    };
    try {
      const saved = await apiFetch("/api/config", { method: "POST", body: newCfg });
      const configured = Boolean(saved.config.base_url && saved.config.model_name);
      setStatus(configured, configured ? "Configured" : "Not configured");
      showToast("Configuration saved.");
    } catch (e) {
      setStatus(false, "Save failed");
      showToast("Save failed: " + e.message);
    }
  });

  // test connection
  document.getElementById("btn-test").addEventListener("click", async () => {
    setStatus(null, "testing…");
    testResult.textContent = "";
    try {
      const result = await apiFetch("/api/test");
      if (result.ok) {
        setStatus(true, "Connection OK");
        testResult.textContent = result.message || "OK";
      } else {
        setStatus(false, result.error || "Failed");
        testResult.textContent = result.error || "Failed";
      }
    } catch (e) {
      setStatus(false, "Error");
      testResult.textContent = "Error: " + e.message;
    }
  });

  // ------------------------------------------------------- humanizer tab
  const srcText = $("#src-text");
  const outText = $("#out-text");
  const outEmpty = $("#out-empty");
  const btnHumanize = $("#btn-humanize");
  const btnStop = $("#btn-stop");
  const btnClearSrc = $("#btn-clear-src");
  const btnClearOut = $("#btn-clear-out");
  const btnCopy = $("#btn-copy");
  const btnDownload = $("#btn-download");
  const btnDownloadMd = $("#btn-download-md");
  const srcCount = $("#src-count");
  const btnDiff = $("#btn-diff");
  const diffStats = $("#diff-stats");
  const diffFoot = $("#diff-foot");
  const outEmptyLabel = outEmpty.querySelector("span");
  const EMPTY_LABEL = outEmptyLabel.textContent;

  let abortCtrl = null;
  let rawBuf = "";
  let hasContent = false;
  let runStartedAt = 0;
  let plainOutput = "";  // source of truth for copy/download/diff
  let diffOn = false;    // "Highlight changes" toggle (off by default)
  let errorSeen = false; // backend sent an \x01E...\x01 error marker
  let boardTimerId = null; // 1s elapsed-time ticker for the status board

  // ---- output helpers ----------------------------------------------------
  function showPlain() {
    outText.textContent = plainOutput;
  }

  function setOutput(text) {
    plainOutput = text;
    if (!text.trim()) outEmptyLabel.textContent = EMPTY_LABEL;
    refreshOutputView();
    updateEmptyState();
  }

  function refreshOutputView() {
    if (diffOn) renderDiff();
    else showPlain();
  }

  function updateEmptyState() {
    outEmpty.hidden = outText.textContent.trim().length > 0;
  }

  function outputText() {
    return plainOutput.trim();
  }

  // ---- diff highlight ----------------------------------------------------
  function setStats(t) {
    diffStats.textContent = t;
    diffFoot.hidden = !t;
  }

  function renderDiff() {
    const out = plainOutput;
    if (!out.trim() || !srcText.value.trim()) {
      showPlain();
      setStats("");
      return;
    }
    const HD = window.HumanizerDiff;
    if (!HD) { showPlain(); setStats(""); return; }
    const runs = HD.diffTexts(srcText.value, out);
    if (!runs) {
      showPlain();
      setStats("text too long to compare");
      return;
    }
    outText.innerHTML = runs.map(r =>
      `<span class="${r.type}">${HD.escapeHtml(r.text)}</span>`).join("");

    let removed = 0, added = 0;
    for (const r of runs) {
      const n = HD.words(r.text);
      if (r.type === "del") removed += n;
      else if (r.type === "ins") added += n;
    }
    if (removed + added === 0) {
      setStats("No changes – text unchanged");
    } else {
      const origWords = HD.words(srcText.value);
      const unchanged = Math.max(0, origWords - removed);
      const pct = origWords ? Math.round(unchanged / origWords * 100) : 100;
      setStats(`−${removed} / +${added} words · ${pct}% unchanged`);
    }
  }

  btnDiff.addEventListener("click", () => {
    diffOn = !diffOn;
    btnDiff.classList.toggle("active", diffOn);
    btnDiff.setAttribute("aria-pressed", String(diffOn));
    refreshOutputView();
  });

  // ---- phase handling (multi-pass: scan/rewrite/check/fix) --------------
  const PHASE_LABELS = {
    scan: "Scanning for AI tells…",
    check: "Checking the rewrite…",
    fix: "Fixing remaining tells…",
  };

  function setPhase(name) {
    // left-pane animation + status line; name "" or "done" resets
    const scanning = name === "scan";
    const verifying = name === "check" || name === "fix";
    srcPane.classList.toggle("scanning", scanning);
    srcPane.classList.toggle("verifying", verifying);
    const label = PHASE_LABELS[name];
    if (label) {
      phaseNote.textContent = label;
      phaseNote.hidden = false;
    } else {
      phaseNote.hidden = true;
    }
  }

  // ---- stream decoding (strips the \x01A\x01 liveness marker) -----------
  function appendText(t) {
    if (!t) return;
    plainOutput += t;
    showPlain(); // always plain while streaming; diff is computed on finish
    hasContent = true;
    updateEmptyState();
    outText.scrollTop = outText.scrollHeight;
  }

  function handleMarker(code) {
    if (code === "A" && !hasContent) {
      const elapsed = (Date.now() - runStartedAt) / 1000;
      outEmptyLabel.textContent = elapsed > 15
        ? "Still thinking – reasoning models can take a while…"
        : "Thinking…";
    } else if (code.startsWith("P")) {
      setPhase(code.slice(1));          // multi-pass phase markers
    } else if (code.startsWith("E")) {
      errorSeen = true;                 // backend error after the 200: show it
      setPhase("");
      showToast(code.slice(1));
    } else if (code.startsWith("M")) {
      boardMsg.textContent = code.slice(1);  // status-board message
    } else if (code === "C") {
      // a fix pass replaced the rewrite: clear and start over
      plainOutput = "";
      outText.textContent = "";
      hasContent = false;
      updateEmptyState();
      outEmptyLabel.textContent = EMPTY_LABEL;
    }
  }

  // Split incoming bytes into plain text and \x01…\x01 markers; markers may
  // straddle chunk boundaries, so keep a small buffer of the tail.
  function feedRaw(text) {
    rawBuf += text;
    let out = "";
    for (;;) {
      const i = rawBuf.indexOf("\u0001");
      if (i === -1) { out += rawBuf; rawBuf = ""; break; }
      out += rawBuf.slice(0, i);
      const j = rawBuf.indexOf("\u0001", i + 1);
      if (j === -1) { rawBuf = rawBuf.slice(i); break; } // wait for marker end
      handleMarker(rawBuf.slice(i + 1, j));
      rawBuf = rawBuf.slice(j + 1);
    }
    appendText(out);
  }

  // ---- word counter ------------------------------------------------------
  function updateSrcCount() {
    const words = (srcText.value || "").match(/\S+/g) || [];
    srcCount.textContent = `${words.length} ${words.length === 1 ? "word" : "words"}`;
  }
  updateSrcCount();
  srcText.addEventListener("input", updateSrcCount);

  // Ctrl+Enter triggers humanize
  srcText.addEventListener("keydown", e => {
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      humanize();
    }
  });

  // ---- run ---------------------------------------------------------------
  function humanize() {
    const text = srcText.value.trim();
    if (!text) {
      showToast("Paste some text first.");
      return;
    }
    if (cfgMultiPass.checked && cfgIntensity.value !== "light") {
      setPhase("scan");   // optimistic: the server confirms with a P marker
    } else {
      setPhase("");
    }
    setRunning(true);
    abortCtrl = new AbortController();
    setOutput("");
    outText.classList.add("loading");
    rawBuf = "";
    hasContent = false;
    runStartedAt = Date.now();
    errorSeen = false;
    outEmptyLabel.textContent = "Thinking…";
    if (cfgMultiPass.checked && cfgIntensity.value !== "light") {
      setPhase("scan");   // optimistic: the server confirms with a P marker
    } else {
      setPhase("");
    }

    fetch(new URL("/api/humanize", apiBase).href, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        intensity: cfgIntensity.value === "light" ? "light" : "thorough",
      }),
      signal: abortCtrl.signal,
    })
    .then(resp => {
      if (!resp.ok) {
        // read the body as text first – it may be JSON (our errors) or not
        return resp.text().then(t => {
          let msg = resp.statusText || "";
          try {
            const j = JSON.parse(t);
            if (j) {
              if (typeof j.error === "string") msg = j.error;
              else if (j.error && j.error.message) msg = j.error.message;
              else if (Array.isArray(j.errors)) msg = j.errors.join(" ");
            }
          } catch (_) { /* not JSON – keep statusText */ }
          if (!msg) msg = t.slice(0, 200) || "request failed";
          return Promise.reject(new Error(`HTTP ${resp.status}: ${msg}`));
        });
      }
      return resp.body.getReader();
    })
    .then(reader => {
      const decoder = new TextDecoder("utf-8", { stream: true });
      function readChunk() {
        return reader.read().then(({ value, done }) => {
          if (!done && value) {
            feedRaw(decoder.decode(value, { stream: true }));
            return readChunk();
          }
          // stream finished
          finishRun(true);
        });
      }
      return readChunk();
    })
    .catch(err => {
      if (err.name !== "AbortError") {
        const isNetwork = err.name === "TypeError" && /fetch|network/i.test(err.message);
        showToast(isNetwork
          ? `Cannot reach the backend at ${apiBase} – is the app running?`
          : "Error: " + err.message);
        // keep any partial text already streamed; empty state shows if none
      }
      finishRun(false);
    });
  }

  function startBoardTimer() {
    stopBoardTimer();
    boardTimerId = setInterval(() => {
      const s = Math.floor((Date.now() - runStartedAt) / 1000);
      boardTimer.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }, 1000);
  }

  function stopBoardTimer() {
    if (boardTimerId) { clearInterval(boardTimerId); boardTimerId = null; }
  }

  function setRunning(running) {
    btnHumanize.disabled = running;
    btnStop.hidden = !running;
    srcText.disabled = running;
    btnDiff.disabled = running;
    srcPane.classList.toggle("running", running);
    if (running) {
      boardMsg.textContent = "Preparing…";
      boardTimer.textContent = "0:00";
      startBoardTimer();
    } else {
      stopBoardTimer();
    }
  }

  function finishRun(completed) {
    outText.classList.remove("loading");
    setRunning(false);
    setPhase("");
    abortCtrl = null;
    rawBuf = "";
    if (!plainOutput.trim()) {
      outEmptyLabel.textContent = EMPTY_LABEL;
      setStats("");
      if (completed && !errorSeen) {
        showToast("The model returned no text. A reasoning model may have spent the whole Max-tokens budget on thinking – raise Max tokens or pick a non-reasoning model.");
      }
    } else if (diffOn && completed) {
      renderDiff(); // user left the highlight toggle on – refresh it
    }
  }

  btnHumanize.addEventListener("click", humanize);

  btnStop.addEventListener("click", () => {
    if (abortCtrl) {
      abortCtrl.abort();
      showToast("Aborted.");
    }
  });

  btnClearSrc.addEventListener("click", () => {
    srcText.value = "";
    updateSrcCount();
    srcText.focus();
  });

  btnClearOut.addEventListener("click", () => setOutput(""));

  // ---- copy / download ---------------------------------------------------
  btnCopy.addEventListener("click", async () => {
    const text = outputText();
    if (!text) { showToast("Nothing to copy yet."); return; }
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.append(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    showToast("Copied!");
  });

  function saveFile(filename, mime) {
    const text = outputText();
    if (!text) { showToast("Nothing to save yet."); return; }
    const blob = new Blob([text], { type: mime + ";charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
    showToast("Saved " + filename);
  }
  btnDownload.addEventListener("click", () => saveFile("humanized.txt", "text/plain"));
  btnDownloadMd.addEventListener("click", () => saveFile("humanized.md", "text/markdown"));

  // ------------------------------------------------------- toast
  const toast = $("#toast");
  const toastMsg = $("#toast-msg");
  let toastTimer = null;
  let toastHide = null;

  function showToast(msg) {
    toastMsg.textContent = msg;
    toast.hidden = false;
    requestAnimationFrame(() => toast.classList.add("show"));
    clearTimeout(toastTimer);
    clearTimeout(toastHide);
    toastTimer = setTimeout(() => {
      toast.classList.remove("show");
      toastHide = setTimeout(() => { toast.hidden = true; }, 300);
    }, 3000);
  }

  // -------------------------------------------------------
  updateEmptyState();
})();
