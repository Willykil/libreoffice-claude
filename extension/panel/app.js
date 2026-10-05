"use strict";

const params = new URLSearchParams(location.search);
const TOKEN = params.get("t") || "";
const firstRun = params.get("run");
const WINDOW_ID = params.get("w");      // set when shown inside LibreOffice's sidebar (claude_embed.py)
if (WINDOW_ID) {
  // How LibreOffice finds this window, plus the height of Edge's title bar in screen pixels,
  // which it cuts off when placing the window in the sidebar.
  const bar = Math.max(0, Math.round((window.outerHeight - window.innerHeight) * window.devicePixelRatio));
  document.title = "Claude-panel-" + WINDOW_ID + "-" + bar;
}
history.replaceState(null, "", "/?t=" + encodeURIComponent(TOKEN) +       // a reload must not re-run it
  (WINDOW_ID ? "&w=" + encodeURIComponent(WINDOW_ID) : ""));

const $ = (id) => document.getElementById(id);
const thread = $("thread"), promptBox = $("prompt"), sendBtn = $("send");
const D = window.ClaudeDiff;
const ACTION_LABELS = { improve: "Improve writing", summarize: "Summarize", explain: "Explain" };
const MODEL_INFO = {
  "": ["Default", "Claude Code's default model"],
  "claude-opus-5-5": ["Opus 5.5", "Best for careful writing and analysis"],
  "claude-sonnet-5-5": ["Sonnet 5.5", "Fast and capable for everyday edits"],
  "claude-haiku-4-5": ["Haiku 4.5", "Fastest, for quick fixes"],
  "claude-fable-5-1": ["Fable 5.1", "Most capable, for the hardest tasks"],
};
const EFFORT_SHORT = { low: "Low", medium: "Medium", high: "High", xhigh: "Extra", max: "Max" };
const EFFORT_TIPS = {
  low: "Low: quick answers with little thinking",
  medium: "Medium: a balance of speed and care",
  high: "High: thinks longer, for harder requests",
  xhigh: "Extra high: thinks even longer",
  max: "Max: as much thinking as it takes, slowest",
};
const TRACKED_ON_TIP = "Tracked changes are on: Claude's edits show up as suggestions you accept or reject one by one " +
  "in Writer (Edit > Track Changes > Manage). Click to turn off.";
const TRACKED_OFF_TIP = "Tracked changes are off: Claude edits the document directly, and Undo reverts it. " +
  "Click to turn on, so each edit shows up as a suggestion you accept or reject.";
const TONE_TIPS = {
  formal: "Rewrite the selected text in a formal, professional tone",
  voice: "Rewrite the selected text the way you write, learned from your writing samples",
};

let state = { doc: null, quick: [], connection: "subscription", models: [], efforts: [], voice: {} };
let busy = false;
let pendingImages = [];    // screenshots waiting to be sent: { media_type, data (base64), url }
let chat = newChat();      // the conversation on screen; saved to history after each reply

function newChat() {
  return { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 7), title: "", doc: "", kind: "",
           messages: [] };
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Claude-Token": TOKEN, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 403) throw new Error("This panel's session has ended. Reopen it from LibreOffice (Claude menu).");
  return res.json();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function icon(name, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  if (cls) svg.setAttribute("class", cls);
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#i-" + name);
  svg.appendChild(use);
  return svg;
}

function button(label, cls, iconName, onClick, tip) {
  const b = el("button", "btn " + (cls || ""));
  b.type = "button";
  if (iconName) b.appendChild(icon(iconName));
  if (label) b.appendChild(document.createTextNode(label));
  if (tip) b.title = tip;
  b.addEventListener("click", onClick);
  return b;
}

function iconButton(name, label, onClick) {
  const b = el("button", "btn quiet");
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.appendChild(icon(name));
  b.addEventListener("click", onClick);
  return b;
}

function toast(text, isError) {
  const t = $("toast");
  t.replaceChildren(...(isError ? [] : [icon("check")]), document.createTextNode(text));
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), isError ? 4000 : 2400);
}

function scrollDown() { thread.scrollTop = thread.scrollHeight; }
const isWriter = () => !!(state.doc && state.doc.kind === "writer");
// Claude writes its replies into Writer and Calc; in Impress and Draw they are copied for now.
const canWrite = (kind) => kind === "writer" || kind === "calc";
const KIND_ICON = { calc: "sheet", impress: "slides", draw: "slides" };
const KIND_NAME = { writer: "Document", calc: "Spreadsheet", impress: "Presentation", draw: "Drawing" };
const WELCOME = {
  calc: "Select some cells, then pick an action below or ask me anything about your workbook.",
  impress: "Pick an action below or ask me anything about your presentation.",
  draw: "Pick an action below or ask me anything about your drawing.",
};
const hasSelection = () => !!(state.doc && state.doc.has_selection);

/* ---------------------------------------------------------------- state */

function modelName(id) { return (MODEL_INFO[id] || [id])[0]; }

function renderState() {
  const doc = state.doc;
  $("context").classList.toggle("has-selection", hasSelection());
  $("contextIcon").replaceChildren(...icon(KIND_ICON[doc && doc.kind] || "doc").childNodes);
  $("docTitle").textContent = doc ? (doc.title || KIND_NAME[doc.kind] || "Document") : "No document";
  $("ctxLabel").textContent = doc ? doc.label : "Open a document, spreadsheet or presentation.";
  $("welcomeSub").textContent = WELCOME[doc && doc.kind]
    || "Select some text, then pick an action below or ask me anything about your document.";
  if (state.connection !== usageFor) { usageFor = state.connection; usage = null; loadUsage(false); }
  renderFootnote();

  // Rewrite as Formal | My voice (Writer)
  $("tone").hidden = !isWriter();
  const voice = state.voice || {};
  for (const b of document.querySelectorAll(".tone-btn")) {
    b.disabled = busy;
    b.classList.toggle("needs-selection", !hasSelection());
    b.setAttribute("aria-disabled", String(busy || !hasSelection()));
    b.title = hasSelection() ? TONE_TIPS[b.dataset.tone] : "Select the text to rewrite first";
    b.classList.toggle("default", b.dataset.tone === "voice" && !!voice.default && !!voice.ready);
  }

  // model · effort selector, tracked tag
  const effort = state.effort_supported ? " · " + (EFFORT_SHORT[state.effort] || state.effort) : "";
  $("selectorLabel").textContent = modelName(state.model || "") + effort;
  // Always shown in Writer, so turning it off by accident doesn't hide the way back.
  const tag = $("trackedTag");
  tag.hidden = !isWriter();
  tag.setAttribute("aria-pressed", String(!!state.track_changes));
  tag.title = state.track_changes ? TRACKED_ON_TIP : TRACKED_OFF_TIP;
  if (!$("popover").hidden) renderPopover();

  const chips = $("chips");
  const key = JSON.stringify([state.quick.map((q) => q.label), hasSelection(), busy]);
  if (chips.dataset.key !== key) {
    chips.dataset.key = key;
    chips.replaceChildren(...state.quick.map((q) => {
      const c = el("button", "chip", q.label);
      c.type = "button";
      const waiting = q.needs_selection && !hasSelection();
      c.disabled = busy;
      c.classList.toggle("needs-selection", waiting);
      c.setAttribute("aria-disabled", String(busy || waiting));
      c.title = waiting ? "Select some text first" : (q.hint || "");
      c.addEventListener("click", () => {
        if (waiting) return toast("Select some text in the document first, then pick " + q.label, true);
        ask({ instruction: q.prompt }, q.label);
      });
      return c;
    }));
  }
  const vline = voice.ready ? "Learned from " + voice.samples + " sample" + (voice.samples === 1 ? "" : "s")
    : (voice.samples ? voice.samples + " sample" + (voice.samples === 1 ? "" : "s") + ", not learned yet" : "Not set up yet");
  $("voiceStatusLine").textContent = vline;
  updateSend();
}

async function poll() {
  try {
    state = await api("/api/state");
    renderState();
    if (state.pending) handleRun(state.pending);
  } catch (e) { /* the office may be busy; try again next tick */ }
}

function handleRun(action) {
  if (action === "settings") openSettings();
  else if (action === "voice") openVoice();
  else if (ACTION_LABELS[action]) ask({ action }, ACTION_LABELS[action]);
  else window.focus();
}

async function saveSetting(body) {
  try {
    await api("/api/settings", body);
    await poll();
  } catch (e) { toast(e.message, true); }
}

/* ---------------------------------------------------------------- the model / effort popover */

function renderPopover() {
  const list = $("modelList");
  const ids = ["", ...state.models.map((m) => m.id)];
  list.replaceChildren(...ids.map((id) => {
    const [name, desc] = MODEL_INFO[id] || [id, ""];
    const b = el("button", "model");
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String((state.model || "") === id));
    const text = el("span", "model-text");
    text.append(el("span", "model-name", name), el("span", "model-desc",
      id === "" && state.connection === "api" ? "Opus 5.5" : desc));
    b.append(text, icon("check", "check"));
    b.addEventListener("click", () => saveSetting({ model: id }));
    return b;
  }));
  const eff = $("effortList");
  eff.classList.toggle("off", !state.effort_supported);
  eff.title = state.effort_supported ? "How long Claude thinks before answering" : "This model has no effort setting";
  eff.replaceChildren(...state.efforts.map((e) => {
    const b = el("button", "", EFFORT_SHORT[e.id] || e.label);
    b.type = "button";
    b.title = EFFORT_TIPS[e.id] || e.label;
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(state.effort === e.id));
    b.addEventListener("click", () => saveSetting({ effort: e.id }));
    return b;
  }));
  $("trackRow").hidden = !isWriter();
  $("trackSwitch").checked = !!state.track_changes;
}

function togglePopover(open) {
  const pop = $("popover");
  const show = open === undefined ? pop.hidden : open;
  pop.hidden = !show;
  $("selector").setAttribute("aria-expanded", String(show));
  if (show) renderPopover();
}

$("selector").addEventListener("click", (e) => { e.stopPropagation(); togglePopover(); });
$("popover").addEventListener("click", (e) => e.stopPropagation());
document.addEventListener("click", () => togglePopover(false));
$("trackSwitch").addEventListener("change", (e) => saveSetting({ track_changes: e.target.checked }));
$("trackedTag").addEventListener("click", () => saveSetting({ track_changes: !state.track_changes }));

/* ---------------------------------------------------------------- citations */

// [P12] / [P12-P14] in Writer answers; [B3], [B2:C9], [Data!C4], ['My sheet'!A1] in Calc answers;
// [S3] / [S3-S5] (slide or page 3) in Impress and Draw answers.
const CITE = /\[(P\d+(?:\s*[-–]\s*P?\d+)?|S\d+(?:\s*[-–]\s*S?\d+)?|(?:(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?\$?[A-Z]{1,3}\$?\d{1,7}(?::\$?[A-Z]{1,3}\$?\d{1,7})?)\]/g;
const stripCites = (t) => t.replace(/\s?\[P\d+(?:\s*[-–]\s*P?\d+)?\]/g, "");

function citeLabel(ref) {
  const m = /^P(\d+)(?:\s*[-–]\s*P?(\d+))?$/.exec(ref);
  if (m) return "¶" + m[1] + (m[2] ? "–" + m[2] : "");
  const s = /^S(\d+)(?:\s*[-–]\s*S?(\d+))?$/.exec(ref);
  if (s) return (state.doc && state.doc.kind === "draw" ? "Page " : "Slide ") + s[1] + (s[2] ? "–" + s[2] : "");
  return ref.replace(/\$/g, "");
}

function richText(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(CITE)) {
    frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    const b = el("button", "cite", citeLabel(m[1]));
    b.type = "button";
    b.dataset.ref = m[1];
    b.title = "Show in the document";
    b.addEventListener("click", async () => {
      const res = await api("/api/goto", { ref: m[1] });
      if (res.error) toast(res.error, true);
    });
    frag.appendChild(b);
    last = m.index + m[0].length;
  }
  frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}

/* ---------------------------------------------------------------- conversation */

// images: the pictures' URLs while they're on screen, or how many there were (in saved chats).
function addUser(text, images) {
  $("welcome").hidden = true;
  document.querySelector(".app").classList.add("chatting");
  const m = el("div", "msg user");
  if (Array.isArray(images) && images.length) {
    const row = el("div", "sent-images");
    for (const url of images) {
      const img = el("img");
      img.src = url;
      img.alt = "Attached image";
      row.appendChild(img);
    }
    m.appendChild(row);
  } else if (images > 0) {
    m.appendChild(el("div", "image-note", images === 1 ? "1 image attached" : images + " images attached"));
  }
  if (text) m.appendChild(el("div", "bubble", text));
  thread.appendChild(m);
  scrollDown();
}

function addThinking() {
  const m = el("div", "msg");
  const card = el("div", "card");
  const row = el("div", "thinking");
  row.append(icon("sparkle", "pulse"), el("span", "label", "Thinking…"), el("span", "elapsed", "0 s"));
  card.appendChild(row);
  m.appendChild(card);
  thread.appendChild(m);
  scrollDown();
  const start = Date.now();
  const timer = setInterval(() => {
    row.querySelector(".elapsed").textContent = Math.floor((Date.now() - start) / 1000) + " s";
  }, 500);
  return { remove() { clearInterval(timer); m.remove(); } };
}

function cardShell(title) {
  const card = el("div", "card");
  const top = el("div", "card-top");
  const t = el("span", "card-title", title);
  top.append(icon("sparkle", "spark"), t, el("span", "spacer"));
  const body = el("div");
  const actions = el("div", "card-actions");
  card.append(top, body, actions);
  return { card, top, title: t, body, actions };
}

function wrapMsg(...nodes) {
  const m = el("div", "msg");
  m.append(...nodes);
  thread.appendChild(m);
  scrollDown();
  return m;
}

function renderGrid(rows) {
  const wrap = el("div", "grid-wrap");
  const table = el("table", "grid");
  for (const r of rows.slice(0, 200)) {
    const tr = el("tr");
    for (const v of r) {
      tr.appendChild(el("td", v.startsWith("=") ? "formula" : (/^-?\d+([.,]\d+)?$/.test(v.trim()) ? "num" : ""), v));
    }
    table.appendChild(tr);
  }
  wrap.appendChild(table);
  return wrap;
}

function copyButton(text) {
  return iconButton("copy", "Copy to the clipboard", async () => {
    try { await navigator.clipboard.writeText(stripCites(text)); toast("Copied"); } catch (e) { toast("Couldn't copy", true); }
  });
}

// Copying from the conversation gives plain text, so a paste takes on the document's own
// formatting instead of the panel's font, size and colors. Paragraph citations are left out.
document.addEventListener("copy", (e) => {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !e.clipboardData) return;
  const active = document.activeElement;
  if (active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")
      && active.selectionStart !== active.selectionEnd) return;     // the message box: already plain
  const box = el("div", "copy-scratch");
  for (let i = 0; i < sel.rangeCount; i++) box.appendChild(sel.getRangeAt(i).cloneContents());
  for (const b of box.querySelectorAll(".cite")) {
    const ref = b.dataset.ref || b.textContent;
    if (/^P\d/.test(ref)) {
      const prev = b.previousSibling;
      if (prev && prev.nodeType === Node.TEXT_NODE) prev.textContent = prev.textContent.replace(/\s$/, "");
      b.remove();
    } else {
      b.replaceWith(b.textContent);
    }
  }
  document.body.appendChild(box);
  const text = box.innerText.replace(/\n{3,}/g, "\n\n").trim();
  box.remove();
  e.clipboardData.setData("text/plain", text);
  e.preventDefault();
});

function addReply(res) {
  if (res.edits) return addEditsCard(res);
  if (res.grid) return addGridCard(res);
  if (res.kind === "writer" && res.selection && res.selection.trim()) {
    const proposed = stripCites(res.text);
    const segs = D.diff(res.selection, proposed);
    const sim = D.similarity(res.selection, segs);
    if (res.tone || sim >= 0.3) return addRewriteCard(res, proposed, segs, sim);
  }
  return addAnswerCard(res);
}

// Questions and drafts: text with clickable citations.
function addAnswerCard(res) {
  const c = cardShell("Claude");
  const body = el("div", "card-body");
  body.appendChild(richText(res.text));
  c.body.appendChild(body);
  if (res.truncated) c.body.appendChild(el("p", "card-note", "The reply was cut off at the length limit."));
  if (canWrite(res.kind)) {
    const replace = button(res.selection ? "Replace selection" : "Insert at cursor", "primary", null,
      () => apply(res.text, "replace", replace, c.card),
      res.selection ? "Put this reply in place of the selected text" : "Put this reply where the cursor is");
    const below = button("Insert below", "", null, () => apply(res.text, "after", below, c.card),
      "Add this reply as a new paragraph after the selection");
    c.actions.append(replace, below);
  }
  c.actions.append(el("span", "spacer"), copyButton(res.text));
  wrapMsg(c.card);
}

// Claude changed the document itself: what it did, what it couldn't, and one Undo for all of it.
function addEditsCard(res) {
  const done = res.edits.done || [], failed = res.edits.failed || [];
  const c = cardShell(done.length ? (done.length === 1 ? "1 edit made" : done.length + " edits made")
                                  : "No edits made");
  if (res.text) {
    const body = el("div", "card-body");
    body.appendChild(richText(res.text));
    c.body.appendChild(body);
  }
  const list = el("ul", "edits");
  for (const line of done) {
    const li = el("li", "ok");
    li.append(icon("check"), el("span", "", line));
    list.appendChild(li);
  }
  for (const line of failed) {
    const li = el("li", "bad");
    li.append(icon("x"), el("span", "", line));
    list.appendChild(li);
  }
  if (done.length || failed.length) c.body.appendChild(list);
  if (res.truncated) c.body.appendChild(el("p", "card-note", "The reply was cut off at the length limit."));
  if (done.length) {
    const note = el("span", "status-note", res.edits.tracked ? "Tracked changes · accept or reject them in Writer"
                                                             : "Ctrl+Z in LibreOffice also undoes them");
    const undo = button("Undo", "", null, async () => {
      let r;
      try { r = await api("/api/undo", {}); } catch (e) { r = { error: e.message }; }
      if (r.error) return toast(r.error, true);
      c.card.classList.add("rejected");
      c.actions.replaceChildren(el("span", "status-note", "Undone — the document is back as it was"));
    }, "Undo every edit from this reply at once");
    c.actions.append(undo, note);
  } else {
    c.actions.remove();
  }
  wrapMsg(c.card);
}

// Calc: new cells previewed as a grid.
function addGridCard(res) {
  const c = cardShell(res.grid.length === 1 ? "1 row to write" : res.grid.length + " rows to write");
  c.body.appendChild(renderGrid(res.grid));
  if (res.truncated) c.body.appendChild(el("p", "card-note", "The reply was cut off at the length limit."));
  const below = button("Write below", "primary", null, () => apply(res.text, "after", below, c.card));
  const at = button("Write at selection", "", null, () => apply(res.text, "replace", at, c.card));
  below.title = "Write into the rows just below the selection";
  at.title = "Write into the sheet starting at the selected cell";
  c.actions.append(below, at, el("span", "spacer"), copyButton(res.text));
  wrapMsg(c.card);
}

// Writer rewrites: Changes (before/after) or Preview (on the page), and One by one.
function addRewriteCard(res, proposed, segs, sim) {
  const c = cardShell("");
  const changes = D.changes(segs);
  if (res.voice) {
    const badge = el("span", "voice-badge");
    badge.append(icon("voice"), document.createTextNode("Your voice"));
    c.top.appendChild(badge);
  }
  const toggle = el("div", "toggle");
  const tChanges = el("button", "", "Changes"), tPreview = el("button", "", "Preview");
  tChanges.title = "Show what changed: removed text struck out, new text highlighted";
  tPreview.title = "Show the rewritten text as it would read in the document";
  for (const b of [tChanges, tPreview]) { b.type = "button"; toggle.appendChild(b); }
  c.top.appendChild(toggle);
  const where = res.paragraph ? "Replace ¶" + res.paragraph + " text" : "Replace selection";
  let view = (changes.length <= 12 && sim >= 0.55) ? "changes" : "preview";
  let accepted = changes.map(() => true);

  function diffView() {
    const box = el("div", "diff");
    for (const s of segs) {
      if ("eq" in s) { box.appendChild(document.createTextNode(s.eq)); continue; }
      if (s.old) box.appendChild(el("del", "d", s.old));
      if (s.new) box.appendChild(el("ins", "d", s.new));
    }
    return box;
  }

  function paper(text) {
    const p = el("div", "paper");
    if (res.before) p.appendChild(el("div", "faint before", "… " + res.before));
    const n = el("div", "new");
    n.appendChild(el("div", "", text));
    p.appendChild(n);
    if (res.after) p.appendChild(el("div", "faint after", res.after + " …"));
    return p;
  }

  function oneByOne() {
    const list = el("div", "hunks");
    const result = el("div");
    const apply3 = button("Apply", "primary", "check", () => apply(D.compose(segs, accepted), "replace", apply3, c.card),
      "Put the changes you kept into the document");
    function refresh() {
      const n = accepted.filter(Boolean).length;
      apply3.lastChild.textContent = n === changes.length ? "Apply all changes" : "Apply " + n + " change" + (n === 1 ? "" : "s");
      apply3.disabled = n === 0;
      result.replaceChildren(paper(D.compose(segs, accepted)));
    }
    let k = 0;
    segs.forEach((s, idx) => {
      if ("eq" in s) return;
      const i = k++;
      const row = el("div", "hunk");
      const text = el("div", "hunk-text");
      const prev = idx > 0 && "eq" in segs[idx - 1] ? segs[idx - 1].eq : "";
      const before = D.tokens(prev);
      const lead = before.slice(-4).join("");
      if (lead.trim()) text.appendChild(el("span", "faint", (before.length > 4 ? "…" : "") + lead));
      if (s.old) text.appendChild(el("del", "d", s.old));
      if (s.new) text.appendChild(el("ins", "d", s.new));
      const yes = el("button", "sq yes"), no = el("button", "sq nope");
      yes.type = no.type = "button";
      yes.setAttribute("aria-label", "Keep this change");
      no.setAttribute("aria-label", "Skip this change");
      yes.title = "Keep this change";
      no.title = "Skip this change and keep your original wording";
      yes.appendChild(icon("check"));
      no.appendChild(icon("x"));
      const sync = () => {
        yes.setAttribute("aria-pressed", String(accepted[i]));
        no.setAttribute("aria-pressed", String(!accepted[i]));
        row.classList.toggle("no", !accepted[i]);
      };
      yes.addEventListener("click", () => { accepted[i] = true; sync(); refresh(); });
      no.addEventListener("click", () => { accepted[i] = false; sync(); refresh(); });
      sync();
      row.append(text, yes, no);
      list.appendChild(row);
    });
    c.title.textContent = "Review changes";
    toggle.hidden = true;
    c.body.replaceChildren(list, el("div", "result-label", "Result"), result);
    c.actions.replaceChildren(apply3, button("Accept all", "", null, () => { accepted = accepted.map(() => true); oneByOne(); },
      "Keep every change"),
      el("span", "spacer"), button("Back", "quiet", null, () => { toggle.hidden = false; render(); },
      "Back to the full suggestion"));
    refresh();
  }

  function render() {
    tChanges.setAttribute("aria-pressed", String(view === "changes"));
    tPreview.setAttribute("aria-pressed", String(view === "preview"));
    c.card.classList.remove("rejected");
    if (view === "changes") {
      c.title.textContent = changes.length === 1 ? "1 change" : changes.length + " changes";
      c.body.replaceChildren(diffView());
      const accept = button("Accept all", "primary", "check", () => apply(proposed, "replace", accept, c.card),
        "Put the rewritten text into the document");
      const reject = button("Reject", "", null, () => {
        c.card.classList.add("rejected");
        c.actions.replaceChildren(el("span", "status-note", "Rejected — nothing was changed"), el("span", "spacer"),
          button("Undo", "quiet", null, render, "Bring the suggestion back"));
      }, "Keep your original text and dismiss the suggestion");
      c.actions.replaceChildren(accept, reject, el("span", "spacer"));
      if (changes.length > 1) c.actions.appendChild(button("One by one", "quiet", null, oneByOne,
        "Go through the changes and choose which to keep"));
    } else {
      c.title.textContent = "Rewritten";
      c.body.replaceChildren(paper(proposed));
      const replace = button(where, "primary", null, () => apply(proposed, "replace", replace, c.card),
        "Put the rewritten text in place of the original");
      const below = button("Insert below", "", null, () => apply(proposed, "after", below, c.card),
        "Add the rewritten text after the original, keeping both");
      c.actions.replaceChildren(replace, below, el("span", "spacer"));
      if (res.tone === "voice") c.actions.appendChild(button("Try Formal", "quiet", null, () => rewrite("formal"), TONE_TIPS.formal));
      if (res.tone === "formal") c.actions.appendChild(button("Try My voice", "quiet", null, () => rewrite("voice"), TONE_TIPS.voice));
      c.actions.appendChild(copyButton(proposed));
    }
    if (res.truncated) c.body.appendChild(el("p", "card-note", "The reply was cut off at the length limit."));
  }
  tChanges.addEventListener("click", () => { view = "changes"; render(); });
  tPreview.addEventListener("click", () => { view = "preview"; render(); });
  render();

  const nodes = [c.card];
  if (res.voice) {
    const foot = el("div", "card-foot");
    const edit = el("button", "link", "Edit");
    edit.type = "button";
    edit.title = "Change your writing samples or what Claude noticed about your style";
    edit.addEventListener("click", openVoice);
    foot.append(icon("voice"), document.createTextNode("Based on your writing profile · " + res.voice_samples +
      " sample" + (res.voice_samples === 1 ? "" : "s") + " · "), edit);
    nodes.push(foot);
  }
  wrapMsg(...nodes);
}

function addError(text, extra) {
  const c = cardShell("Something went wrong");
  c.card.classList.add("error");
  c.body.appendChild(el("div", "card-body", text));
  if (extra && extra.open_settings) c.actions.appendChild(button("Open settings", "", "gear", openSettings));
  else if (extra && extra.open_voice) c.actions.appendChild(button("Set up My voice", "", "voice", openVoice));
  else c.actions.remove();
  wrapMsg(c.card);
}

function historyForApi() {
  return chat.messages.map((m) => ({ role: m.role, text: m.history_text || m.text }));
}

async function ask(body, shownText, images) {
  if (busy) return;
  togglePopover(false);
  images = images || [];
  addUser(shownText || body.instruction, images.map((i) => i.url));
  if (images.length) body.images = images.map((i) => ({ media_type: i.media_type, data: i.data }));
  busy = true;
  renderState();
  const thinking = addThinking();
  body.history = historyForApi();
  let res;
  try {
    res = await api("/api/ask", body);
  } catch (e) {
    res = { error: e.message };
  }
  thinking.remove();
  busy = false;
  if (res.cancelled) {
    thread.lastElementChild.remove();       // drop the question that was stopped
    toast("Stopped");
  } else if (res.error) {
    addError(res.error, res);
    loadUsage(true);         // a limit reached shows up there too
  } else {
    const shown = shownText || body.instruction || "";
    res.tone = body.tone || "";
    // The pictures themselves aren't kept: later turns and saved chats only note that they were there.
    const sent = images.length ? " [" + images.length + " image" + (images.length === 1 ? "" : "s") + " attached]" : "";
    chat.messages.push({ role: "user", text: (body.instruction || shown) + sent, shown, images: images.length },
                       Object.assign({ role: "assistant" }, res));
    if (!chat.title) {
      const title = shown || (images.length === 1 ? "Image" : "Images");
      chat.title = title.length > 80 ? title.slice(0, 77) + "…" : title;
      chat.doc = state.doc ? state.doc.title : "";
      chat.kind = res.kind;
    }
    addReply(res);
    api("/api/history", { save: chat }).catch(() => {});
    loadUsage(true);
    if (res.edits && res.edits.done && res.edits.done.length) poll();      // the selection label may have changed
  }
  renderState();
  promptBox.focus();
}

function rewrite(tone) {
  if (!hasSelection()) return toast("Select the text to rewrite first", true);
  if (tone === "voice" && !(state.voice && state.voice.ready)) {
    openVoice();
    toast("Add some of your own writing first, then let Claude learn it", true);
    return;
  }
  ask({ tone }, tone === "voice" ? "Rewrite · My voice" : "Rewrite · Formal");
}

for (const b of document.querySelectorAll(".tone-btn")) b.addEventListener("click", () => rewrite(b.dataset.tone));

async function apply(text, mode, btn, card, confirmed) {
  let res;
  try {
    res = await api("/api/apply", { text, mode, confirm: !!confirmed });
  } catch (e) {
    return addError(e.message);
  }
  if (res.error) return addError(res.error);
  if (res.confirm) return askToOverwrite(res.confirm, () => apply(text, mode, btn, card, true), card);
  const original = [...btn.childNodes].map((n) => n.cloneNode(true));
  btn.replaceChildren(icon("check"), document.createTextNode(mode === "replace" ? "Done" : "Inserted"));
  btn.classList.add("done");
  setTimeout(() => { btn.replaceChildren(...original); btn.classList.remove("done"); }, 1800);
  toast(res.tracked ? "Added as tracked changes · accept or reject them in Writer"
                    : "Done · Ctrl+Z in LibreOffice undoes it");
}

function askToOverwrite(message, proceed, card) {
  const old = card.querySelector(".confirm");
  if (old) old.remove();
  const bar = el("div", "confirm");
  bar.appendChild(el("p", "", message));
  const yes = button("Replace them", "primary", null, () => { bar.remove(); proceed(); });
  const no = button("Cancel", "", null, () => bar.remove());
  bar.append(yes, no);
  card.appendChild(bar);
  scrollDown();
}

/* ---------------------------------------------------------------- composer */

function updateSend() {
  sendBtn.classList.toggle("stop", busy);
  sendBtn.replaceChildren(icon(busy ? "stop" : "send"));
  sendBtn.setAttribute("aria-label", busy ? "Stop" : "Send");
  sendBtn.title = busy ? "Stop Claude" : "Send (Enter)";
  sendBtn.disabled = !busy && !promptBox.value.trim() && !pendingImages.length;
}

function autosize() {
  promptBox.style.height = "auto";
  promptBox.style.height = Math.min(promptBox.scrollHeight, 160) + "px";
  updateSend();
}

promptBox.addEventListener("input", autosize);
promptBox.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("askForm").requestSubmit();
  }
});
$("askForm").addEventListener("submit", (e) => {
  e.preventDefault();
  if (busy) { api("/api/cancel", {}); return; }
  const text = promptBox.value.trim();
  if (!text && !pendingImages.length) return;
  const images = pendingImages;
  pendingImages = [];
  renderAttachments();
  promptBox.value = "";
  autosize();
  const v = state.voice || {};
  // My voice rewrites the selection; a message with pictures is a question about them.
  ask(isWriter() && v.default && v.ready && !images.length ? { instruction: text, tone: "voice" }
                                                           : { instruction: text }, text, images);
});

function showChat(c) {
  chat = c;
  for (const m of [...thread.querySelectorAll(".msg")]) m.remove();
  $("welcome").hidden = c.messages.length > 0;
  document.querySelector(".app").classList.toggle("chatting", c.messages.length > 0);
  for (const m of c.messages) {
    if (m.role === "user") addUser(m.shown || m.text, m.images || 0);
    else addReply(m);
  }
}

$("newChat").addEventListener("click", () => {
  if (busy) return;
  showChat(newChat());
  promptBox.focus();
});

/* ---------------------------------------------------------------- sheets (settings, history, voice) */

const SHEETS = ["settings", "history", "voice", "usage"];

function openSheet(id) {
  togglePopover(false);
  for (const s of SHEETS) $(s).hidden = s !== id;
  $("scrim").hidden = false;
}

function closeSheets() {
  for (const s of SHEETS) $(s).hidden = true;
  $("scrim").hidden = true;
}

const form = $("settingsForm");

function showFor(backend) {
  for (const n of form.querySelectorAll("[data-show]")) n.hidden = n.dataset.show !== backend;
}

async function openSettings() {
  let s;
  try { s = await api("/api/settings"); } catch (e) { return toast(e.message, true); }
  form.elements.backend.value = s.backend;
  form.elements.instructions_writer.value = s.instructions_writer || "";
  form.elements.instructions_calc.value = s.instructions_calc || "";
  form.elements.instructions_impress.value = s.instructions_impress || "";
  form.elements.instructions_draw.value = s.instructions_draw || "";
  form.elements.claude_path.value = s.claude_path || "";
  form.elements.max_tokens.value = s.max_tokens;
  form.elements.native_sidebar.checked = !!s.native_sidebar;
  $("nativeSidebarRow").hidden = !s.windows;
  form.elements.api_key.value = "";
  form.elements.api_key.placeholder = s.has_api_key ? "Saved — type to replace" : "sk-ant-…";
  $("clearKey").hidden = !s.has_api_key;
  showFor(s.backend);
  openSheet("settings");
}

form.addEventListener("change", (e) => { if (e.target.name === "backend") showFor(e.target.value); });
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form.elements;
  const body = {
    backend: f.backend.value, instructions_writer: f.instructions_writer.value,
    instructions_calc: f.instructions_calc.value, instructions_impress: f.instructions_impress.value,
    instructions_draw: f.instructions_draw.value, claude_path: f.claude_path.value, max_tokens: f.max_tokens.value,
    native_sidebar: f.native_sidebar.checked,
  };
  if (f.api_key.value.trim()) body.api_key = f.api_key.value.trim();
  try { await api("/api/settings", body); } catch (err) { return toast(err.message, true); }
  closeSheets();
  toast("Settings saved");
  poll();
});
$("clearKey").addEventListener("click", async () => {
  await api("/api/settings", { clear_api_key: true });
  form.elements.api_key.placeholder = "sk-ant-…";
  $("clearKey").hidden = true;
  toast("API key removed");
});
$("clearHistory").addEventListener("click", async () => {
  await api("/api/history", { clear: true });
  toast("Chat history cleared");
});
$("openVoiceFromSettings").addEventListener("click", openVoice);

function when(ts) {
  const mins = Math.round((Date.now() / 1000 - ts) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + " min ago";
  if (mins < 60 * 24) return Math.round(mins / 60) + " h ago";
  return new Date(ts * 1000).toLocaleDateString();
}

async function openHistory() {
  let data;
  try { data = await api("/api/history", {}); } catch (e) { return toast(e.message, true); }
  const list = $("historyList");
  const items = data.conversations || [];
  if (!items.length) {
    list.replaceChildren(el("p", "history-empty", "No conversations yet."));
  } else {
    list.replaceChildren(...items.map((c) => {
      const row = el("div", "history-item");
      row.appendChild(icon(KIND_ICON[c.kind] || "doc", "h-icon"));
      const main = el("div", "h-main");
      main.append(el("div", "h-title", c.title || "Untitled conversation"),
                  el("div", "h-meta", [c.doc, when(c.updated)].filter(Boolean).join(" · ")));
      row.appendChild(main);
      const del = el("button", "icon-btn");
      del.type = "button";
      del.title = "Delete this conversation";
      del.setAttribute("aria-label", "Delete conversation");
      del.appendChild(icon("trash"));
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        await api("/api/history", { delete: c.id });
        row.remove();
      });
      row.appendChild(del);
      row.tabIndex = 0;
      row.addEventListener("click", () => { if (!busy) { showChat(c); closeSheets(); } });
      row.addEventListener("keydown", (e) => { if (e.key === "Enter") row.click(); });
      return row;
    }));
  }
  openSheet("history");
}

/* My voice (K) */

const SOURCE = { selection: "From a selection", document: "Whole document", file: "File" };
let voiceData = null;
let learning = false;

function renderVoice(v) {
  voiceData = v;
  const n = v.samples.length;
  let title, sub;
  if (v.ready && !v.stale) {
    title = "Claude can write like you";
    sub = "Learned from " + n + " sample" + (n === 1 ? "" : "s") + " · updated " + when(v.updated);
  } else if (v.ready) {
    title = "Your samples changed";
    sub = "Press Relearn to include them";
  } else if (n) {
    title = "Ready to learn";
    sub = "Press Learn my style. More of your writing gives a better match.";
  } else {
    title = "Teach Claude how you write";
    sub = "Add a few things you wrote yourself. Two or three pages works best.";
  }
  $("voiceTitle").textContent = title;
  $("voiceSub").textContent = sub;
  const list = $("sampleList");
  if (!n) list.replaceChildren(el("div", "samples-empty", "No samples yet."));
  else list.replaceChildren(...v.samples.map((s) => {
    const row = el("div", "sample");
    const text = el("div", "sample-text");
    const meta = s.words.toLocaleString() + " words · " + (SOURCE[s.source] || s.source) +
      (s.partly_used ? " · the first 20,000 characters are used" : "");
    text.append(el("div", "sample-name", s.name), el("div", "sample-meta", meta));
    const rm = iconButton("x", "Remove " + s.name, () => voiceCall({ remove: s.id }));
    row.append(icon("doc"), text, rm);
    return row;
  }));
  const learn = $("learnVoice");
  learn.textContent = learning ? "Learning…" : (v.ready ? "Relearn" : "Learn my style");
  learn.disabled = learning || !n;
  if (document.activeElement !== $("profile")) $("profile").value = v.profile || "";
  $("voiceDefault").checked = !!v.default;
  $("addSelection").disabled = !(isWriter() && hasSelection());
  $("addDocument").disabled = !isWriter();
}

async function voiceCall(body) {
  let v;
  try { v = await api("/api/voice", body); } catch (e) { return toast(e.message, true); }
  if (v.error) toast(v.error, true);
  if (v.samples) renderVoice(v);
  poll();
  return v;
}

async function openVoice() {
  openSheet("voice");
  await voiceCall({});
}

$("closeVoice").addEventListener("click", closeSheets);
$("voiceDone").addEventListener("click", closeSheets);
$("addSelection").addEventListener("click", async () => {
  const v = await voiceCall({ add: "selection" });
  if (v && !v.error) toast("Selection added");
});
$("addDocument").addEventListener("click", async () => {
  const v = await voiceCall({ add: "document" });
  if (v && !v.error) toast("Document added");
});
$("addFiles").addEventListener("click", () => $("fileInput").click());
$("fileInput").addEventListener("change", async (e) => {
  for (const file of [...e.target.files]) {
    const data = await new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result).split(",")[1] || "");
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    }).catch(() => null);
    if (data === null) { toast("Couldn't read " + file.name, true); continue; }
    const v = await voiceCall({ add: "file", name: file.name, data });
    if (v && !v.error) toast(file.name + " added");
  }
  e.target.value = "";
});
$("learnVoice").addEventListener("click", async () => {
  learning = true;
  renderVoice(voiceData);
  try {
    const v = await api("/api/voice", { learn: true });
    learning = false;
    if (v.error) toast(v.error, true);
    else if (v.cancelled) toast("Stopped");
    else toast("Claude learned your style");
    if (v.samples) renderVoice(v);
  } catch (e) {
    learning = false;
    toast(e.message, true);
    renderVoice(voiceData);
  }
  poll();
});
$("profile").addEventListener("change", (e) => voiceCall({ profile: e.target.value }));
$("voiceDefault").addEventListener("change", (e) => voiceCall({ default: e.target.checked }));

$("openSettings").addEventListener("click", openSettings);
$("openHistory").addEventListener("click", openHistory);
$("openUsage").addEventListener("click", openUsage);
$("usageStrip").addEventListener("click", openUsage);
$("closeUsage").addEventListener("click", closeSheets);
$("refreshUsage").addEventListener("click", () => { renderUsage(true); loadUsage(true); });
$("closeSettings").addEventListener("click", closeSheets);
$("closeHistory").addEventListener("click", closeSheets);
$("cancelSettings").addEventListener("click", closeSheets);
$("scrim").addEventListener("click", closeSheets);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeSheets(); togglePopover(false); }
});

/* ---------------------------------------------------------------- screenshots */

const MAX_IMAGES = 5;
const MAX_EDGE = 1568;                 // Claude scales larger pictures down to about this anyway
const MAX_BYTES = 3.5 * 1024 * 1024;   // well under the 5 MB the API takes per picture

function renderAttachments() {
  const box = $("attachments");
  box.hidden = !pendingImages.length;
  box.replaceChildren(...pendingImages.map((img, i) => {
    const t = el("div", "thumb");
    const pic = el("img");
    pic.src = img.url;
    pic.alt = "Image to send";
    const x = el("button", "remove");
    x.type = "button";
    x.title = "Don't send this image";
    x.setAttribute("aria-label", "Remove image");
    x.appendChild(icon("x"));
    x.addEventListener("click", () => { pendingImages.splice(i, 1); renderAttachments(); promptBox.focus(); });
    t.append(pic, x);
    return t;
  }));
  updateSend();
}

function canvasBlob(canvas, type, quality) {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

function dataUrl(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

// Shrinks big pictures so they upload quickly and stay within the API's limits. PNG keeps
// screenshot text crisp; a photo too big as PNG goes as JPEG instead.
async function prepareImage(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height));
  let blob = file;
  if (scale < 1 || file.size > MAX_BYTES || !/^image\/(png|jpeg|gif|webp)$/.test(file.type)) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    blob = await canvasBlob(canvas, "image/png");
    if (blob.size > MAX_BYTES) blob = await canvasBlob(canvas, "image/jpeg", 0.88);
  }
  bitmap.close();
  const url = await dataUrl(blob);
  return { media_type: blob.type, data: url.slice(url.indexOf(",") + 1), url };
}

async function addImages(files) {
  const images = [...files].filter((f) => f.type.startsWith("image/"));
  if (!images.length) return false;
  for (const f of images) {
    if (pendingImages.length >= MAX_IMAGES) { toast("Up to " + MAX_IMAGES + " images per message", true); break; }
    try { pendingImages.push(await prepareImage(f)); } catch (e) { toast("Couldn't read that image", true); }
  }
  renderAttachments();
  promptBox.focus();
  return true;
}

$("attach").addEventListener("click", () => $("imageInput").click());
$("imageInput").addEventListener("change", (e) => { addImages(e.target.files); e.target.value = ""; });
document.addEventListener("paste", (e) => {
  if (!$("settings").hidden || !$("voice").hidden) return;      // pasting a key or a profile there
  const files = [...(e.clipboardData ? e.clipboardData.files : [])];
  if (files.some((f) => f.type.startsWith("image/"))) {
    e.preventDefault();
    addImages(files);
  }
});
const askBox = $("askForm");
askBox.addEventListener("dragover", (e) => {
  if ([...e.dataTransfer.items].some((i) => i.kind === "file")) { e.preventDefault(); askBox.classList.add("dropping"); }
});
askBox.addEventListener("dragleave", () => askBox.classList.remove("dropping"));
askBox.addEventListener("drop", (e) => {
  askBox.classList.remove("dropping");
  if (!e.dataTransfer.files.length) return;
  e.preventDefault();
  addImages(e.dataTransfer.files);
});

/* ---------------------------------------------------------------- hover help */

// Inside LibreOffice the panel is an Edge window that never counts as active, so the browser's own
// title tooltips don't appear there. Draw them here instead: anything with a title gets one.
const tipBox = el("div", "tip");
tipBox.setAttribute("role", "tooltip");
document.body.appendChild(tipBox);
let tipFor = null, tipTimer = 0;

function tipText(t) {
  // Move the title aside so the browser doesn't show its own tooltip as well; code may set it again.
  if (t.hasAttribute("title")) { t.dataset.tip = t.getAttribute("title"); t.removeAttribute("title"); }
  return t.dataset.tip || "";
}

function showTip(t) {
  const text = tipText(t);
  if (!text || !t.isConnected) return hideTip();
  tipBox.textContent = text;
  tipBox.classList.add("show");
  const r = t.getBoundingClientRect(), w = tipBox.offsetWidth, h = tipBox.offsetHeight;
  const x = Math.max(6, Math.min(r.left + r.width / 2 - w / 2, innerWidth - w - 6));
  let y = r.top - h - 6;
  if (y < 6) y = Math.min(r.bottom + 6, innerHeight - h - 6);
  tipBox.style.left = x + "px";
  tipBox.style.top = y + "px";
}

function hideTip() {
  clearTimeout(tipTimer);
  tipFor = null;
  tipBox.classList.remove("show");
}

document.addEventListener("pointerover", (e) => {
  const t = e.target.closest ? e.target.closest("[title], [data-tip]") : null;
  if (t === tipFor) return;
  hideTip();
  if (!t || !tipText(t)) return;
  tipFor = t;
  tipTimer = setTimeout(() => showTip(t), 450);
});
document.documentElement.addEventListener("pointerleave", hideTip);
for (const ev of ["pointerdown", "keydown", "scroll", "wheel"]) document.addEventListener(ev, hideTip, true);
window.addEventListener("blur", hideTip);

/* ---------------------------------------------------------------- usage */

// Subscription: the plan's limits as Claude Code reports them (the same numbers as /usage and the
// desktop app). API key: the per-minute rate limits from the last reply, and this session's tokens.
let usage = null, usageFor = null, usageLoading = false;
const USAGE_EVERY = 2 * 60 * 1000;

async function loadUsage(fresh) {
  if (usageLoading) return;
  usageLoading = true;
  try {
    usage = await api("/api/usage" + (fresh ? "?fresh=1" : ""));
  } catch (e) { /* keep what we had */ }
  usageLoading = false;
  renderFootnote();
  if (!$("usage").hidden) renderUsage();
}

const fmt = (n) => Math.round(n).toLocaleString();
function short(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
  return String(n);
}
function money(n, currency) {
  try { return n.toLocaleString(undefined, { style: "currency", currency: currency || "USD", maximumFractionDigits: n < 1 ? 3 : 2 }); }
  catch (e) { return "$" + n.toFixed(2); }
}
function level(pct, severity) {
  if (severity === "critical" || pct >= 100) return "full";
  if (severity === "warning" || pct >= 80) return "warn";
  return "";
}

// "in 2 h 10 min" for a reset within a day, otherwise "Thu 9:00 AM", like the desktop app.
function resetText(when, prefix) {
  if (!when) return "";
  const t = typeof when === "number" ? when * (when < 1e12 ? 1000 : 1) : Date.parse(when);
  if (!t || isNaN(t)) return "";
  const mins = Math.max(0, Math.round((t - Date.now()) / 60000));
  let text;
  if (mins < 1) text = "in less than a minute";
  else if (mins < 60) text = "in " + mins + " min";
  else if (mins < 24 * 60) text = "in " + Math.floor(mins / 60) + " h" + (mins % 60 ? " " + (mins % 60) + " min" : "");
  else text = new Date(t).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  return (prefix || "Resets") + " " + text;
}

// " · resets 2h 10m", or " · resets Thu" beyond a day: short enough for the bottom line.
function compactReset(when) {
  const t = typeof when === "number" ? when * (when < 1e12 ? 1000 : 1) : Date.parse(when || "");
  if (!t || isNaN(t)) return "";
  const mins = Math.max(0, Math.round((t - Date.now()) / 60000));
  if (mins >= 24 * 60) return " · resets " + new Date(t).toLocaleDateString(undefined, { weekday: "short" });
  return " · resets " + (mins >= 60 ? Math.floor(mins / 60) + "h " : "") + (mins % 60) + "m";
}

function headlineRow(u) {
  const rows = (u && u.plan) || [];
  return rows.find((r) => r.group === "session") || rows[0] || null;
}

// The line under the message box: how the panel connects, or once known, the usage that matters
// most right now with a tiny meter. Clicking it opens the Usage sheet.
function renderFootnote() {
  const u = usage && usage.connection === state.connection ? usage : null;
  let label = "", pct = null, text = "", lvl = "", extra = "", extraLvl = "";
  if (u && u.connection === "api") {
    const rl = u.rate_limits, s = u.session;
    const tight = rl && Object.keys(RATE_LABELS).filter((k) => rl[k] && rl[k].limit)
      .map((k) => ({ k, r: rl[k], left: rl[k].remaining !== undefined ? rl[k].remaining : rl[k].limit }))
      .sort((a, b) => a.left / a.r.limit - b.left / b.r.limit)[0];
    if (tight) {
      label = "API";
      pct = 100 * (tight.r.limit - tight.left) / tight.r.limit;
      lvl = level(pct);
      text = short(tight.left) + (tight.k === "requests" ? " requests" : " tokens") + " left/min";
    }
    if (s && s.requests && s.priced) {
      label = "API";
      text += (text ? " · " : "") + "≈" + money(s.cost_usd);
    }
  } else if (u) {
    const row = headlineRow(u);
    if (row) {
      label = row.group === "session" ? "Session" : row.label;
      pct = row.percent;
      lvl = level(pct, row.severity);
      text = Math.round(pct) + "%" + compactReset(row.resets_at);
      // A weekly limit running low matters more than a quiet session, so it's named too.
      const weekly = (u.plan || []).filter((r) => r !== row && level(r.percent, r.severity))
        .sort((a, b) => b.percent - a.percent)[0];
      if (weekly) {
        extra = "· " + (weekly.label === "All models" ? "Week" : weekly.label) + " " + Math.round(weekly.percent) + "%";
        extraLvl = level(weekly.percent, weekly.severity);
      }
    }
    if (u.status && u.status.status === "rejected") {
      lvl = "full";
      text = "limit reached" + compactReset(u.status.resetsAt);
    }
  }
  if (!label) text = state.connection === "api" ? "Using your Anthropic API key (billed per use)" : "Using your Claude subscription";
  $("usageStrip").className = "usage-strip " + lvl;
  $("stripLabel").textContent = label;
  $("stripMeter").hidden = pct === null;
  $("stripMeter").firstElementChild.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
  $("stripText").textContent = text;
  $("stripExtra").textContent = extra;
  $("stripExtra").className = "strip-extra " + extraLvl;
}

function meterRow(label, value, pct, sub, severity) {
  const row = el("div", "usage-row");
  const line = el("div", "usage-line");
  line.append(el("span", "usage-label", label), el("span", "usage-value", value));
  const meter = el("div", "meter " + level(pct, severity));
  const bar = el("span");
  bar.style.width = Math.max(0, Math.min(100, pct)) + "%";
  meter.appendChild(bar);
  meter.setAttribute("role", "meter");
  meter.setAttribute("aria-valuenow", String(Math.round(pct)));
  meter.setAttribute("aria-valuemin", "0");
  meter.setAttribute("aria-valuemax", "100");
  meter.setAttribute("aria-label", label);
  row.append(line, meter);
  if (sub) row.appendChild(el("span", "usage-sub", sub));
  return row;
}

function section(title, ...children) {
  const g = el("div", "usage-group");
  g.append(el("span", "section-label", title), ...children);
  return g;
}

function stats(pairs) {
  const g = el("div", "usage-stats");
  for (const [k, v] of pairs) g.append(el("span", "k", k), el("span", "v", v));
  return g;
}

function sessionSection(s, withCost) {
  if (!s || !s.requests) return section("This LibreOffice session", el("p", "usage-note", "No requests yet."));
  const pairs = [["Requests", fmt(s.requests)], ["Input tokens", fmt(s.input_tokens)],
                 ["Output tokens", fmt(s.output_tokens)]];
  if (s.cache_read_tokens || s.cache_write_tokens) pairs.push(["Cached tokens", fmt(s.cache_read_tokens + s.cache_write_tokens)]);
  if (withCost) pairs.push(["Estimated cost", s.priced ? money(s.cost_usd) : "Unknown for this model"]);
  return section("This LibreOffice session", stats(pairs));
}

const RATE_LABELS = { requests: "Requests", tokens: "Tokens", "input-tokens": "Input tokens", "output-tokens": "Output tokens" };

function renderUsage(loading) {
  const body = $("usageBody");
  const u = usage && usage.connection === state.connection ? usage : null;
  if (loading || !u) {
    body.replaceChildren(el("p", "usage-loading", "Checking your usage…"));
    return;
  }
  const out = [];
  if (u.connection === "api") {
    const rl = u.rate_limits;
    if (rl) {
      const rows = Object.keys(RATE_LABELS).filter((k) => rl[k]).map((k) => {
        const r = rl[k], left = r.remaining !== undefined ? r.remaining : r.limit;
        return meterRow(RATE_LABELS[k], fmt(left) + " of " + fmt(r.limit) + " left",
                        100 * (r.limit - left) / (r.limit || 1), resetText(r.reset, "Refills"));
      });
      out.push(section("Rate limits, per minute", ...rows));
      if (rl.retry_after) out.push(el("p", "usage-note error", "Rate limited: try again in " + rl.retry_after + " s."));
    } else {
      out.push(section("Rate limits, per minute", el("p", "usage-note", "Shown after your first request.")));
    }
    out.push(sessionSection(u.session, true));
    const note = el("p", "usage-note");
    const link = el("a", "", "console.anthropic.com");
    link.href = "https://console.anthropic.com/usage";
    link.target = "_blank";
    link.rel = "noopener";
    note.append("Your balance and monthly spend are on ", link,
                ". An API key can't read them; the cost above is estimated from list prices.");
    out.push(note);
  } else {
    if (u.error) out.push(el("p", "usage-note error", u.error));
    const rows = u.plan || [];
    const groups = [];
    for (const r of rows) {
      let g = groups.find((x) => x.name === r.group);
      if (!g) groups.push(g = { name: r.group, rows: [] });
      g.rows.push(r);
    }
    const plan = u.subscription ? u.subscription.charAt(0).toUpperCase() + u.subscription.slice(1) + " plan" : "Plan usage";
    for (const g of groups) {
      const title = g.name === "session" ? plan : g.name === "weekly" ? "Weekly limits" : g.name;
      out.push(section(title, ...g.rows.map((r) => meterRow(r.label, Math.round(r.percent) + "% used", r.percent,
                                                            resetText(r.resets_at), r.severity))));
    }
    const ex = u.extra;
    if (ex) {
      const cur = ex.currency;
      const used = ex.used != null ? money(ex.used / 100, cur) : "—";
      const value = ex.limit != null ? used + " of " + money(ex.limit / 100, cur) : used + " spent";
      out.push(section("Extra usage", meterRow("This month", value, ex.percent || 0, "")));
    }
    if (u.status && u.status.status === "rejected") {
      out.push(el("p", "usage-note error", "You've reached a limit. " + resetText(u.status.resetsAt, "It resets") + "."));
    }
    out.push(sessionSection(u.session, false));
    out.push(el("p", "usage-note", "Your limits are shared with claude.ai, the Claude app and Claude Code." +
      (u.at ? " Checked " + when(u.at) + "." : "")));
  }
  body.replaceChildren(...out);
}

async function openUsage() {
  openSheet("usage");
  renderUsage(!usage);
  await loadUsage(false);
}

setInterval(() => loadUsage(false), USAGE_EVERY);
setInterval(renderFootnote, 30 * 1000);      // keeps "resets in" counting down

/* ---------------------------------------------------------------- start */

(async () => {
  await poll();
  // Next check only after this one answered, so checks never pile up on a busy LibreOffice.
  (async function loop() {
    await new Promise((r) => setTimeout(r, 1500));
    await poll();
    loop();
  })();
  window.addEventListener("focus", poll);
  if (firstRun) handleRun(firstRun);
  promptBox.focus();
})();
