"use strict";
const statusLine = document.querySelector("#status");
const details = document.querySelector("#details");
const verification = document.querySelector("#verification");
const controls = document.querySelector("#controls");
const connection = document.querySelector("#connection");
const rooms = document.querySelector("#rooms");
const roomNavigation = document.querySelector("#room-navigation");
const roomPicker = document.querySelector("#room-picker");
roomPicker.addEventListener("change", () => {
  const heading = document.getElementById(roomPicker.value);
  if (!heading || !rooms.contains(heading)) return;
  heading.focus({preventScroll: true});
  heading.scrollIntoView({block: "start"});
  roomPicker.selectedIndex = 0;
});
const entry = window.HouseLightsEntry;
const retryAccess = document.querySelector("#retry-access");
const lights = new Map();
let csrf = "";
let busy = false;
let checking = false;
let approved = false;
let homeReady = false;
let operation = null;
let version = 0;
let lastAccess = 0;
let catalogSignature = "";
let legacyCatalog = false;
let directCommands = false;
function enableForms() {
  document.querySelectorAll("form fieldset").forEach(fieldset => fieldset.disabled = busy || !entry.canSubmit);
  document.querySelector("#entry-recovery").hidden = !entry.recoveryHint;
}
async function request(path, body, timeout = 10000) {
  const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
    headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(timeout)});
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "Access unavailable.");
    error.status = response.status; error.code = result.code;
    throw error;
  }
  if (result.csrf) csrf = result.csrf;
  return result;
}
function connectionMessage(text, error = false) {
  connection.dataset.level = error ? "error" : "info";
  connection.setAttribute("role", error ? "alert" : "status");
  if (connection.textContent !== text) connection.textContent = text;
}
function text(element, value) {
  if (element.textContent !== value) element.textContent = value;
}
function syncControls() {
  if (homeReady && approved) connectionMessage(operation ? "Waiting for " + lights.get(operation.lightId)?.name + "." : "Choose a light. State is checked only when you request it.");
  for (const light of lights.values()) {
    const pending = operation?.lightId === light.light_id;
    const fresh = light.confirmedAt !== null && Date.now() >= light.confirmedAt && Date.now() - light.confirmedAt < 5000;
    const state = fresh ? light.state : light.state === "on" || light.state === "off" ? "unknown" : light.state;
    light.card.dataset.availability = state === "on" || state === "off" ? "online" : state === "unavailable" ? "unavailable" : "unknown";
    text(light.stateLine, pending ? "Checking" : {on: "On", off: "Off", unknown: "Unknown", unavailable: "Unavailable", unchecked: "Not checked"}[state]);
    light.stateLine.dataset.state = light.card.dataset.availability === "online" ? light.state : light.card.dataset.availability;
    text(light.detail, pending ? operation.message : !fresh && light.confirmedAt !== null
      ? "Last confirmed: " + (light.state === "on" ? "On" : "Off") + ". Choose Refresh state to check again."
      : light.message);
    const blocked = !homeReady || busy || operation !== null;
    light.on.disabled = blocked || light.needsRefresh;
    light.off.disabled = blocked || light.needsRefresh;
    light.refresh.disabled = blocked;
    light.on.setAttribute("aria-pressed", String(fresh && !pending && light.state === "on"));
    light.off.setAttribute("aria-pressed", String(fresh && !pending && light.state === "off"));
    light.card.setAttribute("aria-busy", String(pending));
  }
}
function clearControls(message) {
  version += 1;
  operation = null;
  controls.hidden = true;
  lights.clear(); rooms.replaceChildren(); catalogSignature = ""; directCommands = false;
  roomPicker.replaceChildren();
  roomNavigation.hidden = true;
  connectionMessage(message);
}
function catalog(result) {
  const items = Object.hasOwn(result, "lights") ? result.lights : result.light ? [result.light] : null;
  if (!Array.isArray(items) || !items.length || items.length > 32) throw Error("Light controls unavailable.");
  const ids = new Set();
  for (const item of items) {
    if (!item || typeof item.light_id !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(item.light_id)
      || ids.has(item.light_id) || [item.name, item.room].some(value => typeof value !== "string" || !value.trim() || [...value].length > 120 || /[\u0000-\u001f]/.test(value))) {
      throw Error("Light controls unavailable.");
    }
    ids.add(item.light_id);
  }
  return items;
}
function renderCatalog(items, legacy) {
  const ordered = [...items].sort((a, b) => a.room.localeCompare(b.room) || a.name.localeCompare(b.name) || a.light_id.localeCompare(b.light_id));
  const signature = JSON.stringify([legacy, ordered.map(({light_id, name, room}) => [light_id, name, room])]);
  if (signature === catalogSignature) return;
  version += 1; operation = null; lights.clear(); rooms.replaceChildren();
  catalogSignature = signature; legacyCatalog = legacy;
  const prompt = document.createElement("option");
  prompt.value = "";
  prompt.textContent = "Choose a room";
  roomPicker.replaceChildren(prompt);
  const groups = new Map();
  for (const item of ordered) {
    let group = groups.get(item.room);
    if (!group) {
      group = document.createElement("section"); group.className = "room"; group.dataset.room = item.room;
      const heading = document.createElement("h2"); heading.textContent = item.room;
      heading.id = groups.size ? "light-room-" + groups.size : "light-room";
      heading.tabIndex = -1;
      const option = document.createElement("option");
      option.value = heading.id;
      option.textContent = item.room;
      roomPicker.append(option);
      group.append(heading); groups.set(item.room, group); rooms.append(group);
    }
    const card = document.createElement("article"); card.className = "light"; card.dataset.lightId = item.light_id;
    const heading = document.createElement("h3"); heading.textContent = item.name;
    const stateLine = document.createElement("p"); stateLine.className = "state"; stateLine.setAttribute("role", "status");
    const detail = document.createElement("p"); detail.className = "light-detail";
    const actions = document.createElement("div"); actions.className = "actions";
    const on = document.createElement("button"); const off = document.createElement("button"); const refresh = document.createElement("button");
    for (const [button, label, action] of [[on, "On", "on"], [off, "Off", "off"], [refresh, "Refresh state", "read"]]) {
      button.type = "button"; button.textContent = label; button.dataset.action = action;
      button.setAttribute("aria-label", label + ": " + item.name + " (" + item.room + ")");
      button.addEventListener("click", () => startOperation(action, item.light_id));
    }
    stateLine.dataset.role = "state"; detail.dataset.role = "detail"; refresh.className = "refresh-light";
    if (!lights.size) {
      card.id = "light-card"; heading.id = "light-name"; stateLine.id = "light-state";
      on.id = "light-on"; off.id = "light-off"; refresh.id = "refresh-light";
    }
    actions.append(on, off); card.append(heading, stateLine, detail, actions, refresh); group.append(card);
    lights.set(item.light_id, {...item, card, stateLine, detail, on, off, refresh,
      state: "unchecked", confirmedAt: null, needsRefresh: false, message: "Choose Refresh state, On, or Off. State is checked only when requested."});
  }
  roomNavigation.hidden = groups.size < 2;
  syncControls();
}
function show(result) {
  approved = result.status === "approved";
  homeReady = approved && result.home_ready === true;
  directCommands = homeReady && result.direct_commands === true;
  details.hidden = !["anonymous", "invited"].includes(result.status);
  verification.hidden = result.status !== "verify";
  connection.hidden = !approved;
  statusLine.textContent = {anonymous: "Open an invitation, or verify an approved email again.", invited: "Enter your name and email.", verify: "Enter the code from your email.", pending: "Waiting for approval", approved: "Access approved", denied: "Access unavailable. Contact the owner."}[result.status] || "Access unavailable.";
  if (result.status === "invalid_invitation") statusLine.textContent = "Invitation unavailable. Open a current invitation.";
  if (!homeReady) {
    const mismatch = approved && result.home_status === "source_mismatch";
    const message = mismatch
      ? "Error: This connection differs from the last reported home connection. Connect to the home Wi-Fi, then refresh this page."
      : "The home connection is unavailable. Wait and refresh this page. This can take up to 60 seconds.";
    clearControls(message); connectionMessage(message, mismatch);
  } else {
    renderCatalog(catalog(result), !Object.hasOwn(result, "lights"));
    controls.hidden = false;
    connectionMessage("Choose a light. State is checked only when you request it.");
    syncControls();
  }
}
async function submit(path, body) {
  if (busy || !entry.canSubmit) return;
  version += 1; busy = true; enableForms();
  try { show(await request(path, body)); }
  catch (error) {
    statusLine.textContent = error.message;
    if (!error.status || error.status >= 500) { entry.invalidate(); retryAccess.hidden = false; }
  }
  finally { busy = false; enableForms(); syncControls(); }
}
details.addEventListener("submit", event => {event.preventDefault(); submit("api/register", {name: document.querySelector("#name").value, email: document.querySelector("#email").value});});
verification.addEventListener("submit", event => {event.preventDefault(); submit("api/verify", {code: document.querySelector("#code").value});});
document.querySelector("#resend").addEventListener("click", () => submit("api/resend", {}));
function finishUnavailable(light, state, message) {
  operation = null; light.state = state; light.confirmedAt = null; light.needsRefresh = true; light.message = message;
  syncControls();
}
function operationError(error, light) {
  if (error.status === 401 || error.status === 403) {
    homeReady = false; clearControls(error.message + " Check access before another command."); lastAccess = 0;
  } else {
    finishUnavailable(light, "unknown", "Result unconfirmed. Choose Refresh state before another command.");
  }
}
async function startOperation(action, lightId, followUp = null) {
  const light = lights.get(lightId);
  if (busy || operation || !homeReady || !light || (action !== "read" && light.needsRefresh)) return;
  const fresh = light.confirmedAt !== null && Date.now() >= light.confirmedAt && Date.now() - light.confirmedAt < 4000;
  if (action !== "read" && !fresh && !directCommands) { followUp = action; action = "read"; }
  version += 1; busy = true;
  const id = crypto.randomUUID();
  const current = {id, lightId, until: Date.now() + 8000, version, followUp,
    message: action === "read" ? followUp ? "Checking state before turning " + followUp + "." : "Checking current state." : "Waiting for command result."};
  operation = current; syncControls();
  try {
    const body = {operation_id: id, action};
    if (!legacyCatalog) body.light_id = lightId;
    const accepted = await request("api/lights", body, 8000);
    if (operation !== current) return;
    if (accepted.operation_id !== id) throw Error("Result identity is unavailable.");
  } catch (error) {
    if (operation === current) operationError(error, light);
  } finally { busy = false; syncControls(); }
}
async function checkOperation() {
  if (!operation || busy || !homeReady) return;
  const current = operation; const light = lights.get(current.lightId);
  if (Date.now() >= current.until) {
    finishUnavailable(light, "unknown", "Result unconfirmed. Choose Refresh state before another command."); return;
  }
  try {
    const result = await request("api/operations/" + current.id, undefined, Math.max(1, Math.min(3000, current.until - Date.now())));
    if (operation !== current || !light) return;
    if (current.version !== version) {
      finishUnavailable(light, "unknown", "Access changed. Choose Refresh state before another command."); return;
    }
    if (Date.now() >= current.until) {
      finishUnavailable(light, "unknown", "Result arrived too late. Choose Refresh state before another command."); return;
    }
    if (result.operation_id !== current.id) throw Error("Result identity is unavailable.");
    if (result.light_id !== current.lightId && !(legacyCatalog && lights.size === 1 && result.light_id === undefined)) throw Error("Result target is unavailable.");
    if (result.status === "confirmed" && ["on", "off"].includes(result.state)
      && (result.availability === "online" || result.availability === undefined && legacyCatalog && lights.size === 1)) {
      operation = null; light.state = result.state; light.confirmedAt = Date.now(); light.needsRefresh = false;
      light.message = "Confirmed state from this light."; syncControls();
      if (current.followUp) await startOperation(current.followUp, current.lightId);
    } else if (["unknown", "denied", "not_started", "confirmed"].includes(result.status) || Date.now() >= current.until) {
      const unavailable = result.availability === "unavailable" || ["denied", "not_started"].includes(result.status);
      finishUnavailable(light, unavailable ? "unavailable" : "unknown", "Result unavailable. Choose Refresh state before another command.");
    }
  } catch (error) {
    if (operation === current) operationError(error, light);
  }
}
async function refresh() {
  syncControls();
  if (busy || checking || document.hidden) return;
  checking = true; const started = version;
  try {
    if (Date.now() - lastAccess < 3000) {
      if (operation) await checkOperation();
      return;
    }
    lastAccess = Date.now();
    const result = await entry.bootstrap(request, "api/access", "api/invite");
    if (started !== version) return;
    show(result); retryAccess.hidden = true;
    if (operation) await checkOperation();
  } catch {
    if (started !== version) return;
    entry.invalidate(); retryAccess.hidden = false;
    statusLine.textContent = "Access unavailable. The page will check again.";
    homeReady = false; connection.hidden = false;
    clearControls("Home access is unavailable. This page will check again.");
  } finally { checking = false; enableForms(); }
}
document.addEventListener("visibilitychange", () => {
  homeReady = false; clearControls("Checking home access"); lastAccess = 0; refresh();
});
retryAccess.addEventListener("click", () => { lastAccess = 0; refresh(); });
window.addEventListener("house-lights-entry", () => { homeReady = false; clearControls("Checking home access"); lastAccess = 0; enableForms(); refresh(); });
refresh(); setInterval(refresh, 1000); enableForms();
