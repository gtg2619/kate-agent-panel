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
let activity;
let activityTimer;
const summaries = new Map();

function keepActivityLast() {
  if (activity) conversation.append(activity);
}

function working(busy) {
  if (!busy) {
    clearInterval(activityTimer); activityTimer = undefined;
    activity?.remove(); activity = undefined;
    return;
  }
  if (activity) return;
  activity = document.createElement("div"); activity.className = "activity";
  activity.setAttribute("role", "status"); activity.setAttribute("aria-label", "Working");
  const label = document.createElement("span"); label.textContent = "Working";
  const dots = document.createElement("span"); dots.className = "activity-dots";
  dots.setAttribute("aria-hidden", "true"); dots.textContent = ".";
  activity.append(label, dots); keepActivityLast(); empty.hidden = true;
  let count = 1;
  activityTimer = setInterval(() => { count = count % 3 + 1; dots.textContent = ".".repeat(count); }, 450);
  if (followTail) window.scrollTo(0, document.documentElement.scrollHeight);
}

function turnSummary(event) {
  working(false);
  const elapsed = Number(event.elapsed);
  if (!Number.isFinite(elapsed) || elapsed < 0) return;
  let node = summaries.get(event.id);
  if (!node) {
    node = document.createElement("div"); node.className = "turn-summary";
    node.title = "Client-observed wall time. Overlapping tools are counted once; these are not server profiling measurements.";
    summaries.set(event.id, node); conversation.append(node);
    if (summaries.size > 200) { const oldest = summaries.keys().next().value; summaries.get(oldest).remove(); summaries.delete(oldest); }
  }
  const duration = seconds => seconds < .001 ? "<1 ms" : seconds < 1 ? `${Math.round(seconds * 1000)} ms`
    : seconds < 60 ? `${seconds.toFixed(1)} s` : `${Math.floor(seconds / 60)}m ${(seconds % 60).toFixed(1)}s`;
  const labels = {waiting: "Waiting", reasoning: "Reasoning", tools: "Tools", response: "Reply", user: "Awaiting input"};
  const outcome = {stopped: "Stopped", failed: "Failed", completed: "Completed"}[event.outcome] || "Completed";
  const parts = [`${outcome} in ${duration(elapsed)}`];
  for (const [key, label] of Object.entries(labels)) {
    const seconds = Number(event.durations?.[key] || 0);
    if (Number.isFinite(seconds) && seconds > 0 && elapsed > 0)
      parts.push(`${label} ${duration(seconds)} (${seconds / elapsed < .005 ? "<1" : Math.round(seconds / elapsed * 100)}%)`);
  }
  node.textContent = parts.join(" · "); empty.hidden = true;
  if (followTail) window.scrollTo(0, document.documentElement.scrollHeight);
}

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
    keepActivityLast();
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
  keepActivityLast();
}

function answer(id, value, node) {
  if (!nativePanel) return;
  nativePanel.reply(id, JSON.stringify(value));
  for (const control of node.querySelectorAll("button,input,textarea,select")) control.disabled = true;
  node.dataset.waiting = "true";
}

function request(event) {
  const node = document.createElement("section"); node.className = "request";
  const title = document.createElement("strong"); title.textContent = event.title;
  node.append(title);
  if (event.questions?.length) {
    const fields = new Map();
    for (const question of event.questions) {
      const group = document.createElement("fieldset");
      const title = document.createElement("legend"); title.textContent = question.question;
      group.append(title);
      if (question.detail) { const detail = document.createElement("p"); detail.textContent = question.detail; group.append(detail); }
      const choices = [];
      if (question.options?.length) {
        for (const option of question.options) {
          const label = document.createElement("label"); label.className = "choice";
          const input = document.createElement("input");
          input.type = question.multiSelect ? "checkbox" : "radio";
          input.name = `choice-${event.id}-${fields.size}`;
          input._answer = Object.hasOwn(option, "value") ? option.value : option.label;
          const text = document.createElement("span"); text.textContent = option.label;
          if (option.description) { const detail = document.createElement("small"); detail.textContent = option.description; text.append(detail); }
          label.append(input, text); group.append(label); choices.push(input);
        }
      }
      let input;
      if (!choices.length || question.allowOther !== false) {
        input = document.createElement("input");
        input.type = question.isSecret ? "password" : ["number", "integer"].includes(question.valueType) ? "number" : "text";
        if (question.valueType === "integer") input.step = "1";
        if (question.valueType === "number") input.step = "any";
        if (question.valueType === "boolean") {
          input = document.createElement("select");
          for (const [value, text] of [["", "Choose…"], ["true", "Yes"], ["false", "No"]]) {
            const option = document.createElement("option"); option.value = value; option.textContent = text; input.append(option);
          }
        }
        input.setAttribute("aria-label", choices.length ? "Custom answer" : question.question);
        input.placeholder = choices.length ? "Custom answer" : "Answer";
        input.autocomplete = "off";
        input.oninput = () => { if (input.value && !question.multiSelect) for (const choice of choices) choice.checked = false; };
        group.append(input);
        for (const choice of choices) choice.onchange = () => { if (!question.multiSelect) input.value = ""; };
      }
      node.append(group);
      fields.set(question.id, {question, choices, input});
    }
    const submit = document.createElement("button"); submit.textContent = "Reply";
    submit.onclick = () => {
      const answers = {};
      for (const [id, {question, choices, input}] of fields) {
        let values = choices.filter(choice => choice.checked).map(choice => choice._answer);
        if (input?.value.trim()) {
          let value = input.value;
          if (["number", "integer"].includes(question.valueType)) {
            if (!input.reportValidity()) return;
            value = Number(value);
          } else if (question.valueType === "boolean") value = value === "true";
          values = question.multiSelect ? [...values, value] : [value];
        }
        if (!values.length && question.required) {
          (input || choices[0])?.focus();
          let error = node.querySelector(".request-error");
          if (!error) { error = document.createElement("p"); error.className = "request-error"; node.append(error); }
          error.textContent = "Answer all required questions before replying.";
          return;
        }
        answers[id] = question.multiSelect ? values : (values[0] ?? "");
      }
      answer(event.id, answers, node);
    };
    node.append(submit);
    const cancel = document.createElement("button"); cancel.textContent = "Dismiss";
    cancel.onclick = () => answer(event.id, {_cancel: true}, node); node.append(cancel);
  } else if (event.options?.length) {
    for (const option of event.options) {
      const button = document.createElement("button"); button.textContent = option.label;
      button.onclick = () => answer(event.id, option.id, node); node.append(button);
    }
  } else {
    const input = document.createElement("textarea"); node.append(input);
    const button = document.createElement("button"); button.textContent = "Reply";
    button.onclick = () => answer(event.id, input.value, node); node.append(button);
    const cancel = document.createElement("button"); cancel.textContent = "Dismiss";
    cancel.onclick = () => answer(event.id, {_cancel: true}, node); node.append(cancel);
  }
  requests.set(event.id, node); conversation.append(node); empty.hidden = true;
  keepActivityLast();
  if (followTail) node.scrollIntoView({block: "nearest"});
}

function receive(event) {
  if (event.type === "clear") {
    working(false); summaries.clear();
    conversation.replaceChildren(); entries.clear(); requests.clear(); dirty.clear(); empty.hidden = false;
    processGroup = null; turn = 0; followTail = true; window.scrollTo(0, 0);
  } else if (event.type === "status") working(Boolean(event.busy));
  else if (event.type === "turn_summary") turnSummary(event);
  else if (event.type === "message" || event.type === "tool") message(event);
  else if (event.type === "error") message({...event, role: "error", title: "Error", id: `error-${++sequence}`});
  else if (event.type === "request") request(event);
  else if (event.type === "request_resolved") {
    const node = requests.get(event.id);
    if (node) { node.classList.add("resolved"); for (const el of node.querySelectorAll("button,input,textarea,select")) el.disabled = true; }
    requests.delete(event.id);
  } else if (event.type === "request_failed") {
    const node = requests.get(event.id);
    if (node) {
      for (const control of node.querySelectorAll("button,input,textarea,select")) control.disabled = false;
      node.dataset.waiting = "false";
      let error = node.querySelector(".request-error");
      if (!error) { error = document.createElement("p"); error.className = "request-error"; node.append(error); }
      error.textContent = event.text || "Reply failed. Try again.";
    }
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
    if (event.messageGutter > 0) style.setProperty("--message-gutter", `${event.messageGutter}px`);
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
