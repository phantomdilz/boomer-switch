"use strict";

const rooms = document.querySelector("#rooms");
const connection = document.querySelector("#connection");
const cards = new Map();
const busy = new Set();
let polling = false;
let version = 0;
let timer;

async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: body ? {"Content-Type": "application/json"} : {},
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Request unavailable.");
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}

function setState(card, state) {
  const allowed = ["on", "off", "unavailable", "checking"];
  state = allowed.includes(state) ? state : "unavailable";
  const status = card.querySelector("[data-state]");
  status.dataset.state = state;
  const text = {on: "On", off: "Off", unavailable: "Unavailable", checking: "Checking…"}[state];
  if (status.textContent !== text) status.textContent = text;
  for (const button of card.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.action === state));
  }
}

function createCard(light, section) {
  const card = document.createElement("article");
  card.className = "light";
  card.dataset.lightId = light.light_id;
  const header = document.createElement("div");
  header.className = "light-header";
  const name = document.createElement("h3");
  name.textContent = light.name;
  const status = document.createElement("span");
  status.className = "state";
  status.dataset.state = "unavailable";
  status.setAttribute("role", "status");
  header.append(name, status);
  const actions = document.createElement("div");
  actions.className = "actions";
  for (const action of ["on", "off"]) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.action = action;
    button.textContent = action === "on" ? "On" : "Off";
    button.setAttribute("aria-label", `${button.textContent}: ${light.name}`);
    button.addEventListener("click", () => command(light.light_id, action));
    actions.append(button);
  }
  card.append(header, actions);
  section.append(card);
  cards.set(light.light_id, card);
  return card;
}

function showLights(lights) {
  const sections = new Map();
  for (const section of rooms.querySelectorAll("section")) sections.set(section.dataset.room, section);
  for (const light of lights) {
    if (!sections.has(light.room)) {
      const section = document.createElement("section");
      section.dataset.room = light.room;
      const heading = document.createElement("h2");
      heading.textContent = light.room;
      section.append(heading);
      rooms.append(section);
      sections.set(light.room, section);
    }
    const card = cards.get(light.light_id) || createCard(light, sections.get(light.room));
    if (!busy.has(light.light_id)) setState(card, light.state);
  }
}

async function refresh() {
  if (polling || busy.size || document.hidden) return;
  polling = true;
  const startedVersion = version;
  try {
    const result = await request("/api/lights");
    if (!Array.isArray(result.lights)) throw new Error("Lights unavailable.");
    if (startedVersion !== version) return;
    showLights(result.lights);
    connection.textContent = "State comes from each light.";
  } catch {
    if (startedVersion === version) {
      for (const card of cards.values()) setState(card, "unavailable");
      connection.textContent = "Lights unavailable. The page will check again.";
    }
  } finally {
    polling = false;
  }
}

async function command(lightId, action) {
  if (busy.has(lightId)) return;
  version += 1;
  busy.add(lightId);
  const card = cards.get(lightId);
  card.setAttribute("aria-busy", "true");
  for (const button of card.querySelectorAll("button")) button.disabled = true;
  setState(card, "checking");
  try {
    const result = await request(`/api/lights/${encodeURIComponent(lightId)}`, {action});
    setState(card, result.light_id === lightId ? result.state : "unavailable");
  } catch {
    setState(card, "unavailable");
  } finally {
    busy.delete(lightId);
    card.removeAttribute("aria-busy");
    for (const button of card.querySelectorAll("button")) button.disabled = false;
  }
}

function schedule() {
  clearInterval(timer);
  if (!document.hidden) {
    refresh();
    timer = setInterval(refresh, 3000);
  }
}

document.addEventListener("visibilitychange", schedule);
schedule();
