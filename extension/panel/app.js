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
const TONE_TIPS = {
  formal: "Rewrite the selected text in a formal, professional tone",
  voice: "Rewrite the selected text the way you write, learned from your writing samples",
};

let state = { doc: null, quick: [], connection: "subscription", models: [], efforts: [], voice: {} };
let busy = false;
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
  $("connection").textContent = state.connection === "api"
    ? "Using your Anthropic API key (billed per use)"
    : "Using your Claude subscription";

  // Rewrite as Formal | My voice (Writer)
  $("tone").hidden = !isWriter();
  const voice = state.voice || {};
  for (const b of document.querySelectorAll(".tone-btn")) {
    b.disabled = busy || !hasSelection();
    b.title = hasSelection() ? TONE_TIPS[b.dataset.tone] : "Select the text to rewrite first";
    b.classList.toggle("default", b.dataset.tone === "voice" && !!voice.default && !!voice.ready);
  }

  // model · effort selector, tracked tag
  const effort = state.effort_supported ? " · " + (EFFORT_SHORT[state.effort] || state.effort) : "";
  $("selectorLabel").textContent = modelName(state.model || "") + effort;
  $("trackedTag").hidden = !(isWriter() && state.track_changes);
  if (!$("popover").hidden) renderPopover();

  const chips = $("chips");
  const key = JSON.stringify([state.quick.map((q) => q.label), hasSelection(), busy]);
  if (chips.dataset.key !== key) {
    chips.dataset.key = key;
    chips.replaceChildren(...state.quick.map((q) => {
      const c = el("button", "chip", q.label);
      c.type = "button";
      c.disabled = busy || (q.needs_selection && !hasSelection());
      c.title = q.needs_selection && !hasSelection() ? "Select some text first" : (q.hint || "");
      c.addEventListener("click", () => ask({ instruction: q.prompt }, q.label));
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
$("trackedTag").addEventListener("click", () => saveSetting({ track_changes: false }));

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

function addUser(text) {
  $("welcome").hidden = true;
  document.querySelector(".app").classList.add("chatting");
  const m = el("div", "msg user");
  m.appendChild(el("div", "bubble", text));
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
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (e) { toast("Couldn't copy", true); }
  });
}

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

async function ask(body, shownText) {
  if (busy) return;
  togglePopover(false);
  addUser(shownText || body.instruction);
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
  } else {
    const shown = shownText || body.instruction;
    res.tone = body.tone || "";
    chat.messages.push({ role: "user", text: body.instruction || shown, shown },
                       Object.assign({ role: "assistant" }, res));
    if (!chat.title) {
      chat.title = shown.length > 80 ? shown.slice(0, 77) + "…" : shown;
      chat.doc = state.doc ? state.doc.title : "";
      chat.kind = res.kind;
    }
    addReply(res);
    api("/api/history", { save: chat }).catch(() => {});
    if (res.edits && res.edits.done && res.edits.done.length) poll();      // the selection label may have changed
  }
  renderState();
  promptBox.focus();
}

function rewrite(tone) {
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
  sendBtn.disabled = !busy && !promptBox.value.trim();
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
  if (!text) return;
  promptBox.value = "";
  autosize();
  const v = state.voice || {};
  ask(isWriter() && v.default && v.ready ? { instruction: text, tone: "voice" } : { instruction: text });
});

function showChat(c) {
  chat = c;
  for (const m of [...thread.querySelectorAll(".msg")]) m.remove();
  $("welcome").hidden = c.messages.length > 0;
  document.querySelector(".app").classList.toggle("chatting", c.messages.length > 0);
  for (const m of c.messages) {
    if (m.role === "user") addUser(m.shown || m.text);
    else addReply(m);
  }
}

$("newChat").addEventListener("click", () => {
  if (busy) return;
  showChat(newChat());
  promptBox.focus();
});

/* ---------------------------------------------------------------- sheets (settings, history, voice) */

const SHEETS = ["settings", "history", "voice"];

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
$("closeSettings").addEventListener("click", closeSheets);
$("closeHistory").addEventListener("click", closeSheets);
$("cancelSettings").addEventListener("click", closeSheets);
$("scrim").addEventListener("click", closeSheets);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeSheets(); togglePopover(false); }
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
