"use strict";

const params = new URLSearchParams(location.search);
const TOKEN = params.get("t") || "";
const firstRun = params.get("run");
history.replaceState(null, "", "/?t=" + encodeURIComponent(TOKEN));   // a reload must not re-run it

const $ = (id) => document.getElementById(id);
const thread = $("thread"), promptBox = $("prompt"), sendBtn = $("send");
const ACTION_LABELS = { improve: "Improve writing", summarize: "Summarize", explain: "Explain" };

let state = { doc: null, quick: [], connection: "subscription", models: [], efforts: [] };
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

function button(label, cls, iconName, onClick) {
  const b = el("button", "btn " + (cls || ""));
  b.type = "button";
  if (iconName) b.appendChild(icon(iconName));
  if (label) b.appendChild(document.createTextNode(label));
  b.addEventListener("click", onClick);
  return b;
}

function toast(text) {
  const t = $("toast");
  t.replaceChildren(icon("check"), document.createTextNode(text));
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2400);
}

function scrollDown() { thread.scrollTop = thread.scrollHeight; }

/* ---------------------------------------------------------------- state */

function fillSelect(select, items, value) {
  const key = JSON.stringify(items);
  if (select.dataset.key !== key) {
    select.dataset.key = key;
    select.replaceChildren(...items.map((i) => new Option(i.label, i.id)));
  }
  if (document.activeElement !== select) select.value = value;
}

function renderState() {
  const doc = state.doc;
  $("context").classList.toggle("has-selection", !!(doc && doc.has_selection));
  $("contextIcon").replaceChildren(...icon(doc && doc.kind === "calc" ? "sheet" : "doc").childNodes);
  $("docTitle").textContent = doc ? (doc.title || (doc.kind === "calc" ? "Spreadsheet" : "Document")) : "No document";
  $("ctxLabel").textContent = doc ? doc.label : "Open a Writer document or Calc spreadsheet.";
  $("welcomeSub").textContent = doc && doc.kind === "calc"
    ? "Select some cells, then pick an action below or ask me anything about your workbook."
    : "Select some text, then pick an action below or ask me anything about your document.";
  $("connection").textContent = state.connection === "api"
    ? "Using your Anthropic API key (billed per use)"
    : "Using your Claude subscription";

  // model / effort / tracked changes
  fillSelect($("modelPick"), [{ id: "", label: "Default" }, ...state.models], state.model || "");
  fillSelect($("effortPick"), state.efforts, state.effort);
  $("effortPill").classList.toggle("disabled", !state.effort_supported);
  $("effortPill").title = state.effort_supported
    ? "Effort: how long Claude thinks before answering"
    : "This model doesn't have an effort setting";
  const track = $("trackToggle");
  track.hidden = !(doc && doc.kind === "writer");
  track.setAttribute("aria-pressed", String(!!state.track_changes));

  const chips = $("chips");
  const key = JSON.stringify([state.quick.map((q) => q.label), doc && doc.has_selection, busy]);
  if (chips.dataset.key !== key) {
    chips.dataset.key = key;
    chips.replaceChildren(...state.quick.map((q) => {
      const c = el("button", "chip", q.label);
      c.type = "button";
      c.disabled = busy || (q.needs_selection && !(doc && doc.has_selection));
      if (q.needs_selection && !(doc && doc.has_selection)) c.title = "Select some text first";
      c.addEventListener("click", () => ask({ instruction: q.prompt }, q.label));
      return c;
    }));
  }
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
  else if (ACTION_LABELS[action]) ask({ action }, ACTION_LABELS[action]);
  else window.focus();
}

async function saveSetting(body) {
  try {
    await api("/api/settings", body);
    await poll();
  } catch (e) { addError(e.message); }
}

$("modelPick").addEventListener("change", (e) => saveSetting({ model: e.target.value }));
$("effortPick").addEventListener("change", (e) => saveSetting({ effort: e.target.value }));
$("trackToggle").addEventListener("click", async () => {
  const on = !state.track_changes;
  await saveSetting({ track_changes: on });
  toast(on ? "Edits will be tracked changes" : "Edits go straight into the document");
});

/* ---------------------------------------------------------------- citations */

// [P12] / [P12-P14] in Writer answers; [B3], [B2:C9], [Data!C4], ['My sheet'!A1] in Calc answers.
const CITE = /\[(P\d+(?:\s*[-–]\s*P?\d+)?|(?:(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?\$?[A-Z]{1,3}\$?\d{1,7}(?::\$?[A-Z]{1,3}\$?\d{1,7})?)\]/g;

function citeLabel(ref) {
  const m = /^P(\d+)(?:\s*[-–]\s*P?(\d+))?$/.exec(ref);
  if (m) return "¶" + m[1] + (m[2] ? "–" + m[2] : "");
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
      if (res.error) toast(res.error);
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

function addReply(res) {
  const m = el("div", "msg");
  const card = el("div", "card");
  const head = el("div", "card-head");
  head.append(icon("sparkle"), document.createTextNode("Claude"));
  card.appendChild(head);
  if (res.grid) card.appendChild(renderGrid(res.grid));
  else {
    const body = el("div", "card-body");
    body.appendChild(richText(res.text));
    card.appendChild(body);
  }
  if (res.truncated) card.appendChild(el("p", "card-note", "The reply was cut off at the length limit."));

  const calc = res.kind === "calc";
  const actions = el("div", "card-actions");
  const primary = button(calc ? "Write at selection" : "Replace selection", "primary", null,
    () => apply(res.text, "replace", primary, card));
  const secondary = button(calc ? "Write below" : "Insert below", "", null,
    () => apply(res.text, "after", secondary, card));
  primary.title = calc ? "Write into the sheet starting at the selected cell" : "Replace the selected text (or insert at the cursor)";
  secondary.title = calc ? "Write into the rows just below the selection" : "Insert as new paragraphs after the selection";
  const copy = button("Copy", "quiet", "copy", async () => {
    try { await navigator.clipboard.writeText(res.text); toast("Copied"); } catch (e) { toast("Couldn't copy"); }
  });
  actions.append(primary, secondary, el("span", "spacer"), copy);
  card.appendChild(actions);
  m.appendChild(card);
  thread.appendChild(m);
  scrollDown();
}

function addError(text, openSettingsLink) {
  const m = el("div", "msg");
  const card = el("div", "card error");
  const head = el("div", "card-head");
  head.append(icon("sparkle"), document.createTextNode("Something went wrong"));
  card.append(head, el("div", "card-body", text));
  if (openSettingsLink) {
    const actions = el("div", "card-actions");
    actions.appendChild(button("Open settings", "", "gear", openSettings));
    card.appendChild(actions);
  }
  m.appendChild(card);
  thread.appendChild(m);
  scrollDown();
}

function historyForApi() {
  return chat.messages.map((m) => ({ role: m.role, text: m.text }));
}

async function ask(body, shownText) {
  if (busy) return;
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
    addError(res.error, res.open_settings);
  } else {
    const shown = shownText || body.instruction;
    chat.messages.push({ role: "user", text: body.instruction || shown, shown },
                       { role: "assistant", text: res.text, kind: res.kind, grid: res.grid, truncated: res.truncated });
    if (!chat.title) {
      chat.title = shown.length > 80 ? shown.slice(0, 77) + "…" : shown;
      chat.doc = state.doc ? state.doc.title : "";
      chat.kind = res.kind;
    }
    addReply(res);
    api("/api/history", { save: chat }).catch(() => {});
  }
  renderState();
  promptBox.focus();
}

async function apply(text, mode, btn, card, confirmed) {
  let res;
  try {
    res = await api("/api/apply", { text, mode, confirm: !!confirmed });
  } catch (e) {
    return addError(e.message);
  }
  if (res.error) return addError(res.error);
  if (res.confirm) return askToOverwrite(res.confirm, () => apply(text, mode, btn, card, true), card);
  const original = btn.textContent;
  btn.replaceChildren(icon("check"), document.createTextNode(mode === "replace" ? "Done" : "Inserted"));
  btn.classList.add("done");
  setTimeout(() => { btn.textContent = original; btn.classList.remove("done"); }, 1800);
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
  ask({ instruction: text });
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

/* ---------------------------------------------------------------- sheets (settings, history) */

function openSheet(id) {
  for (const s of ["settings", "history"]) $(s).hidden = s !== id;
  $("scrim").hidden = false;
}

function closeSheets() {
  $("settings").hidden = true;
  $("history").hidden = true;
  $("scrim").hidden = true;
}

const form = $("settingsForm");

function showFor(backend) {
  for (const n of form.querySelectorAll("[data-show]")) n.hidden = n.dataset.show !== backend;
}

async function openSettings() {
  let s;
  try { s = await api("/api/settings"); } catch (e) { return addError(e.message); }
  form.elements.backend.value = s.backend;
  form.elements.instructions_writer.value = s.instructions_writer || "";
  form.elements.instructions_calc.value = s.instructions_calc || "";
  form.elements.claude_path.value = s.claude_path || "";
  form.elements.max_tokens.value = s.max_tokens;
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
    instructions_calc: f.instructions_calc.value, claude_path: f.claude_path.value, max_tokens: f.max_tokens.value,
  };
  if (f.api_key.value.trim()) body.api_key = f.api_key.value.trim();
  try { await api("/api/settings", body); } catch (err) { return addError(err.message); }
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

function when(ts) {
  const mins = Math.round((Date.now() / 1000 - ts) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return mins + " min ago";
  if (mins < 60 * 24) return Math.round(mins / 60) + " h ago";
  return new Date(ts * 1000).toLocaleDateString();
}

async function openHistory() {
  let data;
  try { data = await api("/api/history", {}); } catch (e) { return addError(e.message); }
  const list = $("historyList");
  const items = data.conversations || [];
  if (!items.length) {
    list.replaceChildren(el("p", "history-empty", "No conversations yet."));
  } else {
    list.replaceChildren(...items.map((c) => {
      const row = el("div", "history-item");
      row.appendChild(icon(c.kind === "calc" ? "sheet" : "doc", "h-icon"));
      const main = el("div", "h-main");
      main.append(el("div", "h-title", c.title || "Untitled conversation"),
                  el("div", "h-meta", [c.doc, when(c.updated)].filter(Boolean).join(" · ")));
      row.appendChild(main);
      const del = el("button", "icon-btn");
      del.type = "button";
      del.title = "Delete";
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

$("openSettings").addEventListener("click", openSettings);
$("openHistory").addEventListener("click", openHistory);
$("closeSettings").addEventListener("click", closeSheets);
$("closeHistory").addEventListener("click", closeSheets);
$("cancelSettings").addEventListener("click", closeSheets);
$("scrim").addEventListener("click", closeSheets);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheets(); });

/* ---------------------------------------------------------------- start */

(async () => {
  await poll();
  setInterval(poll, 1500);
  window.addEventListener("focus", poll);
  if (firstRun) handleRun(firstRun);
  promptBox.focus();
})();
