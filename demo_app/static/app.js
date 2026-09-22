// TaskBoard client. No framework, no build step.
// "/" is the buggy build, "/?bugs=off" the clean build. The same ?bugs=... is added to every request.
const bugsParam = new URLSearchParams(location.search).get("bugs");
const BUGGY = bugsParam !== "off";

// "/?inject=on" is T7's prompt-injection canary (D7 point 4): forwarded the same way as bugs=...,
// so every request (not just the page load) tells the server to include the canary task.
const injectParam = new URLSearchParams(location.search).get("inject");

let tasks = [];
let filter = "all";

const form = document.getElementById("add-form");
const input = document.getElementById("new-title");
const errorBox = document.getElementById("form-error");
const list = document.getElementById("task-list");
const emptyNote = document.getElementById("empty-note");
const itemsLeft = document.getElementById("items-left");

// ---- talking to the server ------------------------------------------------

function api(path, method = "GET", body = undefined) {
  const url = new URL(path, location.origin);
  if (bugsParam !== null) url.searchParams.set("bugs", bugsParam);
  if (injectParam !== null) url.searchParams.set("inject", injectParam);
  const options = { method };
  if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  return fetch(url, options);
}

async function errorText(res) {
  try {
    const data = await res.json();
    if (typeof data.detail === "string") return data.detail;
  } catch (e) { /* body was not JSON */ }
  return `Something went wrong (HTTP ${res.status}).`;
}

// Tell the server that a seeded issue just showed on screen (ground truth for the evaluation).
const reported = new Set();
function trigger(id, detail) {
  const key = id + "|" + detail;
  if (reported.has(key)) return;
  reported.add(key);
  api("/__trigger", "POST", { id, detail });
}

async function load() {
  const res = await api("/api/tasks");
  tasks = await res.json();
  render();
}

// ---- user actions ---------------------------------------------------------

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function hideError() {
  errorBox.hidden = true;
  errorBox.textContent = "";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const res = await api("/api/tasks", "POST", { title: input.value });
  if (!res.ok) {
    // SEEDED S5: in the buggy build a 409 (duplicate title) is swallowed; the user sees nothing.
    if (!(BUGGY && res.status === 409)) showError(await errorText(res));
    return;
  }
  input.value = "";
  hideError();
  await load();
  input.focus();
});

input.addEventListener("input", hideError);

async function setDone(id, done) {
  const res = await api(`/api/tasks/${id}`, "PATCH", { done });
  if (!res.ok) showError(await errorText(res));
  await load(); // always show what the server says
}

async function deleteTask(id) {
  const res = await api(`/api/tasks/${id}`, "DELETE");
  if (!res.ok) {
    // SEEDED S1: in the buggy build the failed delete is swallowed; the user sees nothing.
    if (!BUGGY) showError(await errorText(res));
    return;
  }
  hideError();
  await load();
}

document.querySelectorAll(".filters button").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    render();
  });
});

// ---- drawing the page -----------------------------------------------------

function taskRow(task) {
  const li = document.createElement("li");

  const label = document.createElement("label");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = task.done;
  box.addEventListener("change", () => setDone(task.id, box.checked));
  const title = document.createElement("span");
  title.className = "title";
  title.textContent = task.title;
  if (BUGGY && task.title.length > 60) {
    // SEEDED S4: long titles get the no-wrap style (see style.css); the trigger is in render()
    title.classList.add("nowrap");
  }
  label.append(box, title);

  const del = document.createElement("button");
  del.type = "button";
  del.textContent = "Delete";
  del.setAttribute("aria-label", `Delete ${task.title}`);
  del.addEventListener("click", () => deleteTask(task.id));

  li.append(label, del);
  return li;
}

function render() {
  document.querySelectorAll(".filters button").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.filter === filter));
  });

  const visible = tasks.filter((t) =>
    filter === "all" || (filter === "active" ? !t.done : t.done));
  list.replaceChildren(...visible.map(taskRow));
  emptyNote.hidden = visible.length > 0;

  const active = tasks.filter((t) => !t.done).length;
  let shown = active;
  if (BUGGY && tasks.some((t) => t.done)) {
    // SEEDED S6: once a task is completed the footer shows the total instead of the active count.
    shown = tasks.length;
    trigger("S6", `footer shows ${shown}, active tasks are ${active}`);
  }
  itemsLeft.textContent = `${shown} ${shown === 1 ? "item" : "items"} left`;

  const root = document.documentElement;
  if (BUGGY && list.querySelector(".nowrap") && root.scrollWidth > root.clientWidth) {
    // SEEDED S4 trigger: logged only when the page really overflows sideways, not merely
    // when a title is long (reading scrollWidth forces the browser to lay out the page first).
    trigger("S4", `page is ${root.scrollWidth}px wide in a ${root.clientWidth}px viewport`);
  }
}

load();
