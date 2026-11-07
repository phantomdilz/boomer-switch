"use strict";
const rooms = document.querySelector("#rooms");
const connection = document.querySelector("#connection");
const signIn = document.querySelector("#sign-in");
let busy = false;
let timer;
function setBusy(value) {
  busy = value;
  rooms.setAttribute("aria-busy", String(value));
  for (const button of rooms.querySelectorAll("button")) {
    const state = button.closest("article").querySelector("[data-state]").dataset.state;
    button.disabled = value || !["on", "off"].includes(state);
  }
}
async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
      headers: body ? {"Content-Type": "application/json"} : {},
      body: body ? JSON.stringify(body) : undefined, signal: controller.signal});
    if (!response.ok) {
      signIn.hidden = response.status !== 403;
      throw new Error("Unavailable");
    }
    return await response.json();
  } finally { clearTimeout(timeout); }
}
function show(light) {
  rooms.replaceChildren();
  const section = document.createElement("section");
  const room = document.createElement("h2");
  room.textContent = light.room;
  const card = document.createElement("article");
  card.className = "light";
  card.dataset.lightId = light.light_id;
  const name = document.createElement("h3");
  name.textContent = light.name;
  const state = document.createElement("p");
  state.dataset.state = light.state;
  state.textContent = {on: "On", off: "Off"}[light.state] || "Unavailable";
  state.setAttribute("role", "status");
  const actions = document.createElement("div");
  actions.className = "actions";
  for (const action of ["on", "off"]) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.action = action;
    button.textContent = action === "on" ? "On" : "Off";
    button.disabled = !["on", "off"].includes(light.state);
    button.setAttribute("aria-pressed", String(light.state === action));
    button.addEventListener("click", () => command(light.light_id, action));
    actions.append(button);
  }
  card.append(name, state, actions);
  section.append(room, card);
  rooms.append(section);
}
function unavailable() {
  rooms.replaceChildren();
  connection.textContent = "Unavailable. Check guest Wi-Fi and website permission.";
}
async function refresh() {
  if (busy || document.hidden) return;
  setBusy(true);
  connection.textContent = "Checking lamp…";
  try {
    const result = await request("/api/lights");
    if (!Array.isArray(result.lights) || result.lights.length !== 1) throw new Error("Unavailable");
    show(result.lights[0]);
    signIn.hidden = true;
    connection.textContent = ["on", "off"].includes(result.lights[0].state)
      ? "Confirmed state from your lamp." : "Unavailable. The page will check again.";
  } catch { unavailable(); } finally { setBusy(false); }
}
async function command(id, action) {
  if (busy) return;
  setBusy(true);
  connection.textContent = "Checking lamp…";
  try {
    const result = await request(`/api/lights/${encodeURIComponent(id)}`, {action, operation_id: crypto.randomUUID()});
    if (result.light_id !== id) throw new Error("Unavailable");
    const old = rooms.querySelector("h2");
    const name = rooms.querySelector("h3");
    show({...result, room: old.textContent, name: name.textContent});
    connection.textContent = ["on", "off"].includes(result.state)
      ? "Confirmed state from your lamp." : "Unavailable. Check current state before another command.";
  } catch { unavailable(); } finally { setBusy(false); }
}
signIn.addEventListener("click", async () => {
  if (busy) return;
  setBusy(true);
  signIn.disabled = true;
  try { window.location.assign((await request("/auth/start", {})).url); }
  catch { unavailable(); signIn.disabled = false; }
  finally { setBusy(false); }
});
function schedule() {
  clearInterval(timer);
  if (!document.hidden) { refresh(); timer = setInterval(refresh, 10000); }
}
document.addEventListener("visibilitychange", schedule);
schedule();
