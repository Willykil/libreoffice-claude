"use strict";

const params = new URLSearchParams(location.search);
const TOKEN = params.get("t") || "";
const firstRun = params.get("run");
history.replaceState(null, "", "/?t=" + encodeURIComponent(TOKEN));   // a reload must not re-run it

const $ = (id) => document.getElementById(id);
const thread = $("thread"), promptBox = $("prompt"), sendBtn = $("send");
const ACTION_LABELS = { improve: "Improve writing", summarize: "Summarize", explain: "Explain" };

let state = { doc: null, quick: [], connection: "subscription" };
let conversation = [];     // [{role: "user"|"assistant", text}] sent back for follow-ups
let busy = false;

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
  toast.timer = setTimeout(() => t.classList.remove("show"), 1800);
}

function scrollDown() { thread.scrollTop = thread.scrollHeight; }

/* ---------------------------------------------------------------- state */

function renderState() {
  const doc = state.doc;
  const ctx = $("context");
  ctx.classList.toggle("has-selection", !!(doc && doc.has_selection));
  $("contextIcon").replaceChildren(...icon(doc && doc.kind === "calc" ? "sheet" : "doc").childNodes);
  $("docTitle").textContent = doc ? (doc.title || (doc.kind === "calc" ? "Spreadsheet" : "Document")) : "No document";
  $("ctxLabel").textContent = doc ? doc.label : "Open a Writer document or Calc spreadsheet.";
  $("welcomeSub").textContent = doc && doc.kind === "calc"
    ? "Select some cells, then pick an action below or ask me anything about your data."
    : "Select some text, then pick an action below or ask me anything about your document.";
  $("connection").textContent = state.connection === "api"
    ? "Using your Anthropic API key (billed per use)"
    : "Using your Claude subscription";

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
      const td = el("td", v.startsWith("=") ? "formula" : (/^-?\d+([.,]\d+)?$/.test(v.trim()) ? "num" : ""), v);
      tr.appendChild(td);
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
  else card.appendChild(el("div", "card-body", res.text));
  if (res.truncated) card.appendChild(el("p", "card-note", "The reply was cut off at the length limit."));

  const calc = res.kind === "calc";
  const actions = el("div", "card-actions");
  const primary = button(calc ? "Write at selection" : "Replace selection", "primary", null,
    () => apply(res.text, "replace", primary));
  const secondary = button(calc ? "Write below" : "Insert below", "", null,
    () => apply(res.text, "after", secondary));
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

async function ask(body, shownText) {
  if (busy) return;
  addUser(shownText || body.instruction);
  busy = true;
  renderState();
  const thinking = addThinking();
  body.history = conversation.slice();
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
    conversation.push({ role: "user", text: body.instruction || shownText }, { role: "assistant", text: res.text });
    addReply(res);
  }
  renderState();
  promptBox.focus();
}

async function apply(text, mode, btn) {
  try {
    const res = await api("/api/apply", { text, mode });
    if (res.error) return addError(res.error);
    const original = btn.textContent;
    btn.replaceChildren(icon("check"), document.createTextNode(mode === "replace" ? "Done" : "Inserted"));
    btn.classList.add("done");
    setTimeout(() => { btn.textContent = original; btn.classList.remove("done"); }, 1800);
    toast("Done · Ctrl+Z in LibreOffice undoes it");
  } catch (e) {
    addError(e.message);
  }
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
$("newChat").addEventListener("click", () => {
  if (busy) return;
  conversation = [];
  for (const m of [...thread.querySelectorAll(".msg")]) m.remove();
  $("welcome").hidden = false;
  document.querySelector(".app").classList.remove("chatting");
  promptBox.focus();
});

/* ---------------------------------------------------------------- settings */

const form = $("settingsForm");

function showFor(backend) {
  for (const n of form.querySelectorAll("[data-show]")) n.hidden = n.dataset.show !== backend;
}

async function openSettings() {
  let s;
  try { s = await api("/api/settings"); } catch (e) { return addError(e.message); }
  const models = form.elements.model;
  models.replaceChildren(new Option("Default", ""), ...s.models.map((m) => new Option(m, m)));
  if (s.model && !s.models.includes(s.model)) models.appendChild(new Option(s.model, s.model));
  models.value = s.model || "";
  form.elements.backend.value = s.backend;
  form.elements.effort.value = s.effort;
  form.elements.extra_instructions.value = s.extra_instructions || "";
  form.elements.claude_path.value = s.claude_path || "";
  form.elements.max_tokens.value = s.max_tokens;
  form.elements.api_key.value = "";
  form.elements.api_key.placeholder = s.has_api_key ? "Saved — type to replace" : "sk-ant-…";
  $("clearKey").hidden = !s.has_api_key;
  showFor(s.backend);
  $("settings").hidden = false;
  $("scrim").hidden = false;
}

function closeSettings() {
  $("settings").hidden = true;
  $("scrim").hidden = true;
}

form.addEventListener("change", (e) => { if (e.target.name === "backend") showFor(e.target.value); });
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = form.elements;
  const body = {
    backend: f.backend.value, model: f.model.value, extra_instructions: f.extra_instructions.value,
    claude_path: f.claude_path.value, max_tokens: f.max_tokens.value,
  };
  if (f.effort.value) body.effort = f.effort.value;
  if (f.api_key.value.trim()) body.api_key = f.api_key.value.trim();
  try { await api("/api/settings", body); } catch (err) { return addError(err.message); }
  closeSettings();
  toast("Settings saved");
  poll();
});
$("clearKey").addEventListener("click", async () => {
  await api("/api/settings", { clear_api_key: true });
  form.elements.api_key.placeholder = "sk-ant-…";
  $("clearKey").hidden = true;
  toast("API key removed");
});
$("openSettings").addEventListener("click", openSettings);
$("closeSettings").addEventListener("click", closeSettings);
$("cancelSettings").addEventListener("click", closeSettings);
$("scrim").addEventListener("click", closeSettings);
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("settings").hidden) closeSettings(); });

/* ---------------------------------------------------------------- start */

(async () => {
  await poll();
  setInterval(poll, 1500);
  window.addEventListener("focus", poll);
  if (firstRun) handleRun(firstRun);
  promptBox.focus();
})();
