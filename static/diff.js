// Humanizer – word-level diff engine (no DOM dependencies, testable in node)
//
// Strategy: recursive diff – first split both texts into paragraphs
// (blocks), diff those cheaply, then word-diff only the changed block
// pairs. Keeps the DP matrices small even for long texts.
//
// Public API (window.HumanizerDiff):
//   diffTexts(original, output) -> Array<{type:'eq'|'del'|'ins', text:string}>
//                                 or null when the text is too large to diff
//   escapeHtml(s)               -> string

(function (root) {
  "use strict";

  // Largest word-DP allowed for a single block pair (cells = (n+1)*(m+1)).
  var MAX_CELLS = 4000000;

  // ---- helpers ----------------------------------------------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function words(text) {
    var m = text.match(/\S+/g);
    return m ? m.length : 0;
  }

  // Split into words, each carrying the whitespace that follows it, plus
  // any leading whitespace of the text. Rendering a token as lead+w+ws
  // reproduces the source formatting exactly.
  function wordTokens(text) {
    var parts = String(text).split(/(\s+)/);
    var toks = [];
    var lead = "";
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (!p) continue;
      if (/^\s+$/.test(p)) {
        if (toks.length === 0) lead += p;
        else toks[toks.length - 1].ws += p;
      } else {
        toks.push({ w: p, ws: "" });
      }
    }
    return { lead: lead, toks: toks };
  }

  // Split into paragraphs while keeping the blank-line separators as
  // their own elements so they take part in the block diff.
  function splitBlocks(text) {
    return String(text).split(/(\n[ \t]*\n)/);
  }

  // ---- generic LCS diff over {w:...} tokens -----------------------------
  // Returns [{t:'eq',a,b},{t:'del',a},{t:'ins',b}] or null when too large.
  function lcsOps(a, b) {
    var n = a.length, m = b.length;
    if ((n + 1) * (m + 1) > MAX_CELLS) return null;
    var W = m + 1;
    var dp = new Uint32Array((n + 1) * W);
    for (var i = n - 1; i >= 0; i--) {
      var row = i * W, next = (i + 1) * W;
      for (var j = m - 1; j >= 0; j--) {
        dp[row + j] = a[i].w === b[j].w
          ? dp[next + j + 1] + 1
          : (dp[next + j] >= dp[row + j + 1] ? dp[next + j] : dp[row + j + 1]);
      }
    }
    var ops = [];
    var x = 0, y = 0;
    while (x < n && y < m) {
      if (a[x].w === b[y].w) { ops.push({ t: "eq", a: x, b: y }); x++; y++; }
      else if (dp[(x + 1) * W + y] >= dp[x * W + y + 1]) { ops.push({ t: "del", a: x }); x++; }
      else { ops.push({ t: "ins", b: y }); y++; }
    }
    while (x < n) { ops.push({ t: "del", a: x }); x++; }
    while (y < m) { ops.push({ t: "ins", b: y }); y++; }
    return ops;
  }

  function pushRun(runs, type, text) {
    if (!text) return;
    var last = runs[runs.length - 1];
    if (last && last.type === type) last.text += text;
    else runs.push({ type: type, text: text });
  }

  // ---- word diff for one changed block pair -----------------------------
  function diffBlockPair(origBlock, outBlock, runs) {
    if (origBlock === outBlock) { pushRun(runs, "eq", outBlock); return; }
    var A = wordTokens(origBlock);
    var B = wordTokens(outBlock);
    var ops = lcsOps(A.toks, B.toks);
    if (!ops) { // too big for a fine diff – show the block as replaced
      pushRun(runs, "del", origBlock);
      pushRun(runs, "ins", outBlock);
      return;
    }
    var first = true;
    for (var i = 0; i < ops.length; i++) {
      var op = ops[i];
      if (op.t === "eq") {
        var te = B.toks[op.b];
        pushRun(runs, "eq", (first ? B.lead : "") + te.w + te.ws);
      } else if (op.t === "ins") {
        var ti = B.toks[op.b];
        pushRun(runs, "ins", (first ? B.lead : "") + ti.w + ti.ws);
      } else {
        var td = A.toks[op.a];
        pushRun(runs, "del", (first ? A.lead : "") + td.w + td.ws);
      }
      first = false;
    }
  }

  // ---- main entry point -------------------------------------------------
  function diffTexts(original, output) {
    var A = splitBlocks(original);
    var B = splitBlocks(output);
    var aTok = [], bTok = [], i;
    for (i = 0; i < A.length; i++) aTok.push({ w: A[i] });
    for (i = 0; i < B.length; i++) bTok.push({ w: B[i] });
    var bops = lcsOps(aTok, bTok);
    if (!bops) return null;

    var runs = [];
    var k = 0;
    while (k < bops.length) {
      var op = bops[k];
      if (op.t === "eq") { pushRun(runs, "eq", B[op.b]); k++; continue; }
      // collect the whole changed segment (consecutive del/ins)
      var dels = [], inss = [];
      while (k < bops.length && bops[k].t !== "eq") {
        if (bops[k].t === "del") dels.push(A[bops[k].a]);
        else inss.push(B[bops[k].b]);
        k++;
      }
      var pairs = Math.min(dels.length, inss.length);
      for (var p = 0; p < pairs; p++) diffBlockPair(dels[p], inss[p], runs);
      for (p = pairs; p < dels.length; p++) pushRun(runs, "del", dels[p]);
      for (p = pairs; p < inss.length; p++) pushRun(runs, "ins", inss[p]);
    }
    return runs;
  }

  var api = { diffTexts: diffTexts, escapeHtml: escapeHtml, words: words };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.HumanizerDiff = api;
})(typeof window !== "undefined" ? window : globalThis);
