"use strict";
const renderer = createAgentRenderer(markdownit, katex);
const conversation = document.getElementById("conversation");
const empty = document.getElementById("empty");
const entries = new Map();
const requests = new Map();
let nativePanel;
let scheduled = false;
const dirty = new Set();
let sequence = 0;
let turn = 0;
let processGroup;
let followTail = true;

window.addEventListener("scroll", () => {
  followTail = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 32;
}, {passive: true});

function processing() {
  if (!processGroup) {
    const node = document.createElement("details"); node.className = "process";
    const label = document.createElement("summary"); label.textContent = "Process";
    const body = document.createElement("div"); body.className = "process-body";
    node.append(label, body); conversation.append(node);
    // Opening process details should leave the clicked row in place while reading.
    label.addEventListener("click", () => { followTail = false; });
    processGroup = {node, body};
  }
  return processGroup;
}

function renderSoon(entry) {
  dirty.add(entry);
  if (scheduled) return;
  scheduled = true;
  setTimeout(() => {
    for (const item of dirty) {
      if (item.role === "diagnostic" || item.role === "tool") item.body.textContent = item.text;
      else item.body.innerHTML = renderer.render(item.text);
    }
    dirty.clear(); scheduled = false;
    if (followTail) window.scrollTo(0, document.documentElement.scrollHeight);
  }, 50);
}

function message(event) {
  const role = event.type === "tool" ? "tool" : (event.role || "assistant");
  const folded = ["reasoning", "diagnostic", "tool"].includes(role);
  const id = `${folded ? `${turn}:` : ""}${role}:${event.id || `event-${++sequence}`}`;
  let entry = entries.get(id);
  if (!entry) {
    if (role === "user") { turn++; processGroup = null; }
    const group = folded ? processing() : null;
    const node = document.createElement("article");
    node.className = `message ${role}`;
    const label = document.createElement("header");
    label.textContent = event.title || ({user: "You", assistant: "Agent", reasoning: "Thinking", diagnostic: "Process output", notice: "Notice"}[role] || "Tool");
    const body = document.createElement(["diagnostic", "tool"].includes(role) ? "pre" : "div");
    body.className = "body";
    node.append(label, body); (group ? group.body : conversation).append(node);
    for (const attachment of event.attachments || []) {
      const details = document.createElement("details"); details.className = "attachment";
      const summary = document.createElement("summary"); summary.textContent = attachment.title;
      const preview = document.createElement("pre");
      try {
        const snapshot = JSON.parse(attachment.text);
        const link = document.createElement("a"); link.textContent = snapshot.file;
        link.href = `agent-file:${encodeURIComponent(snapshot.file)}`;
        summary.replaceChildren(link);
        preview.textContent = snapshot.text;
      } catch (_) { preview.textContent = attachment.text; }
      details.append(summary, preview); node.append(details);
    }
    entry = {node, label, body, role, group, text: ""}; entries.set(id, entry);
    empty.hidden = true;
    // Bound retained DOM and content for long tool-heavy sessions.
    if (entries.size > 1000) {
      const oldest = entries.keys().next().value;
      const removed = entries.get(oldest);
      removed.node.remove(); dirty.delete(removed); entries.delete(oldest);
      if (removed.group && !removed.group.body.childElementCount) {
        removed.group.node.remove();
        if (processGroup === removed.group) processGroup = null;
      }
    }
  }
  entry.text = ((event.append ? entry.text : "") + (event.text || "")).slice(-2 * 1024 * 1024);
  if (event.status) entry.label.textContent = `${event.title || "Tool"} · ${event.status}`;
  renderSoon(entry);
}

function answer(id, value, node) {
  if (!nativePanel) return;
  nativePanel.reply(id, JSON.stringify(value));
  for (const control of node.querySelectorAll("button,input,textarea,select")) control.disabled = true;
}

function request(event) {
  const node = document.createElement("section"); node.className = "request";
  const title = document.createElement("strong"); title.textContent = event.title;
  node.append(title);
  if (event.questions?.length) {
    const fields = new Map();
    for (const question of event.questions) {
      const label = document.createElement("label"); label.textContent = question.question;
      const input = document.createElement("input"); input.type = question.isSecret ? "password" : "text";
      if (question.options?.length) {
        const list = document.createElement("datalist"); list.id = `choices-${event.id}-${fields.size}`;
        for (const option of question.options) { const el = document.createElement("option"); el.value = option.label; el.textContent = option.description; list.append(el); }
        input.setAttribute("list", list.id); node.append(list);
      }
      label.append(input); node.append(label); fields.set(question.id, input);
    }
    const submit = document.createElement("button"); submit.textContent = "Reply";
    submit.onclick = () => answer(event.id, Object.fromEntries([...fields].map(([id, input]) => [id, input.value])), node);
    node.append(submit);
  } else if (event.options?.length) {
    for (const option of event.options) {
      const button = document.createElement("button"); button.textContent = option.label;
      button.onclick = () => answer(event.id, option.id, node); node.append(button);
    }
  } else {
    const input = document.createElement("textarea"); node.append(input);
    const button = document.createElement("button"); button.textContent = "Reply";
    button.onclick = () => answer(event.id, input.value, node); node.append(button);
  }
  requests.set(event.id, node); conversation.append(node); empty.hidden = true;
}

function receive(event) {
  if (event.type === "clear") {
    conversation.replaceChildren(); entries.clear(); requests.clear(); dirty.clear(); empty.hidden = false;
    processGroup = null; turn = 0; followTail = true; window.scrollTo(0, 0);
  } else if (event.type === "message" || event.type === "tool") message(event);
  else if (event.type === "error") message({...event, role: "notice", id: `error-${Date.now()}-${entries.size}`});
  else if (event.type === "request") request(event);
  else if (event.type === "request_resolved") {
    const node = requests.get(event.id);
    if (node) { node.classList.add("resolved"); for (const el of node.querySelectorAll("button,input,textarea,select")) el.disabled = true; }
  } else if (event.type === "requests_cleared") {
    for (const node of requests.values()) node.remove(); requests.clear();
  } else if (event.type === "theme") {
    const style = document.documentElement.style;
    for (const key of ["background", "foreground", "accent", "muted", "alternate", "border", "selection", "selectedText"]) {
      if (event[key]) style.setProperty(`--${key}`, event[key]);
    }
    if (event.fontFamily) style.setProperty("--font-family", `${JSON.stringify(event.fontFamily)}, sans-serif`);
    if (event.fixedFontFamily) style.setProperty("--fixed-font-family", `${JSON.stringify(event.fixedFontFamily)}, monospace`);
    if (event.fontSize > 0) style.setProperty("--font-size", `${event.fontSize}px`);
    if (event.colorScheme) style.colorScheme = event.colorScheme;
  }
}

document.addEventListener("click", event => {
  const link = event.target.closest("a");
  if (!link) return;
  event.preventDefault();
  const href = link.getAttribute("href");
  if (nativePanel && href && !/^(javascript|data|vbscript):/i.test(href)) nativePanel.openLink(href);
});

if (typeof qt !== "undefined" && qt.webChannelTransport) {
  new QWebChannel(qt.webChannelTransport, channel => {
    nativePanel = channel.objects.panel;
    nativePanel.transcriptEvent.connect(json => receive(JSON.parse(json)));
    nativePanel.rendererReady();
  });
}
// Also available to the local rendering smoke test, without a native channel.
globalThis.agentPanelReceive = receive;
