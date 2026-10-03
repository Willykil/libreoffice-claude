"use strict";
// Word-level comparison of the selection and Claude's rewrite, for the Changes and
// One-by-one views. Works in the panel (window.ClaudeDiff) and in Node (module.exports).

(function (root) {
  const TOKEN = /\s+|[\p{L}\p{N}’'_-]+|[^\s\p{L}\p{N}]/gu;
  const MAX_CELLS = 4000000;      // LCS table size; past this, show one whole-text change

  function tokens(text) {
    return text.match(TOKEN) || [];
  }

  // Segments: {eq: "text"} or {old: "text", new: "text"} (either side may be "").
  function diff(before, after) {
    const a = tokens(before), b = tokens(after);
    if (a.length * b.length > MAX_CELLS) {
      return before === after ? [{ eq: before }] : [{ old: before, new: after }];
    }
    // lcs[i][j] = LCS length of a[i:], b[j:]
    const lcs = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
    for (let i = a.length - 1; i >= 0; i--) {
      for (let j = b.length - 1; j >= 0; j--) {
        lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
    }
    const segs = [];
    const push = (kind, text) => {
      const last = segs[segs.length - 1];
      if (kind === "eq") {
        if (last && "eq" in last) last.eq += text; else segs.push({ eq: text });
      } else {
        if (!last || "eq" in last) segs.push({ old: "", new: "" });
        segs[segs.length - 1][kind] += text;
      }
    };
    let i = 0, j = 0;
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && a[i] === b[j]) { push("eq", a[i]); i++; j++; }
      else if (j < b.length && (i === a.length || lcs[i][j + 1] >= lcs[i + 1][j])) { push("new", b[j]); j++; }
      else { push("old", a[i]); i++; }
    }
    return absorbSpaces(segs);
  }

  // "a b c" -> "a x c" reads better as one change "b"->"x" than as several split by a lone
  // space: fold an all-whitespace equal run between two changes into them.
  function absorbSpaces(segs) {
    const out = [];
    for (let k = 0; k < segs.length; k++) {
      const s = segs[k];
      const prev = out[out.length - 1], next = segs[k + 1];
      if ("eq" in s && /^\s+$/.test(s.eq) && s.eq.length <= 2 && prev && !("eq" in prev) && next && !("eq" in next)) {
        prev.old += s.eq + next.old;
        prev.new += s.eq + next.new;
        k++;
      } else {
        out.push("eq" in s ? { eq: s.eq } : { old: s.old, new: s.new });
      }
    }
    return out;
  }

  function changes(segs) {
    return segs.filter((s) => !("eq" in s));
  }

  // The text after applying only the accepted changes (accepted: array of booleans, one per change).
  function compose(segs, accepted) {
    let k = 0;
    return segs.map((s) => ("eq" in s ? s.eq : (accepted[k++] ? s.new : s.old))).join("");
  }

  // Share of the original kept as-is, 0..1. Low means a rewrite rather than an edit.
  function similarity(before, segs) {
    const kept = segs.reduce((n, s) => n + ("eq" in s ? s.eq.trim().length : 0), 0);
    const total = Math.max(before.replace(/\s+/g, "").length, 1);
    return Math.min(1, kept / total);
  }

  const api = { diff, changes, compose, similarity, tokens };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ClaudeDiff = api;
})(typeof window !== "undefined" ? window : globalThis);
