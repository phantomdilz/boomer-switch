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
let automaticUpdates = false;
let stateGeneration = null;
class UpdateClock {
  constructor(read, apply, failed) {
    this.read = read; this.apply = apply; this.failed = failed;
    this.active = false; this.pending = null; this.epoch = 0; this.next = 0; this.delay = 2000;
  }
  setActive(active) {
    if (active === this.active) return;
    this.active = active; this.interrupt();
  }
  interrupt() { this.epoch += 1; this.next = Date.now() + this.delay; }
  async tick() {
    if (!this.active || this.pending || Date.now() < this.next || busy || operation) return;
    const current = {epoch:this.epoch}; this.pending = current;
    try {
      const result = await this.read();
      if (!this.active || current.epoch !== this.epoch) return;
      this.delay = 2000;
      this.apply(result);
    } catch (error) {
      if (!this.active || current.epoch !== this.epoch) return;
      this.delay = Math.min(30000, this.delay * 2);
      this.failed(error);
    } finally {
      if (this.pending === current) this.pending = null;
      this.next = Date.now() + this.delay;
    }
  }
}
function preferredLights() {
  return [...lights.values()].filter(light => {
    const rect = light.card.getBoundingClientRect?.();
    return rect && rect.bottom > 0 && rect.top < window.innerHeight;
  }).map(light => light.light_id);
}
const updates = new UpdateClock(
  () => request("api/lights", {operation_id:crypto.randomUUID(), action:"read", background:true, preferred:preferredLights()}, 3000),
  result => { lastAccess = Date.now(); show(result); },
  () => {
    homeReady = false; lastAccess = 0;
    clearControls("State updates are unavailable. Checking home access again.");
  });
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
  if (homeReady && approved) connectionMessage(operation ? "Waiting for " + lights.get(operation.lightId)?.name + "." : automaticUpdates ? "States update automatically while this page is visible." : "Choose a light. State is checked only when you request it.");
  for (const light of lights.values()) {
    const pending = operation?.lightId === light.light_id;
    const fresh = light.confirmedAt !== null && Date.now() >= light.confirmedAt && Date.now() - light.confirmedAt < (automaticUpdates ? 45000 : 5000);
    const state = fresh ? light.state : light.state === "on" || light.state === "off" ? "stale" : light.state;
    light.card.dataset.availability = state === "on" || state === "off" ? "online" : state === "stale" ? "stale" : state === "unavailable" ? "unavailable" : "unknown";
    text(light.stateLine, pending ? "Checking" : state === "stale" ? light.state === "on" ? "On" : "Off" : {on: "On", off: "Off", unknown: "Unknown", unavailable: "Unavailable", unchecked: "Not checked"}[state]);
    light.stateLine.dataset.state = state;
    const last = light.lastConfirmed;
    const elapsed = last && Date.now() >= last.at ? Math.floor((Date.now()-last.at)/1000) : null;
    text(light.detail, pending ? operation.message : elapsed !== null
      ? "Last confirmed: " + (last.state === "on" ? "On" : "Off") + " · " + elapsed + "s ago." + (state === "stale" ? " Stale; waiting for an update." : ["unknown","unavailable"].includes(state) ? " Current state is " + state + "." : "")
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
  updates.setActive(false); automaticUpdates = false; stateGeneration = null;
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
  updates.interrupt(); version += 1; operation = null; lights.clear(); rooms.replaceChildren();
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
      state: "unchecked", confirmedAt: null, lastConfirmed:null, reportSample:null, uncertain:false, recoveryBarrier:null, needsRefresh: false, message: "Waiting for a device report. You can also choose Refresh state, On, or Off."});
  }
  roomNavigation.hidden = groups.size < 2;
  syncControls();
}
function showStates(result) {
  if (!Number.isSafeInteger(result.state_generation) || result.state_generation < 1 || !Number.isFinite(result.state_time) || !Array.isArray(result.states) || result.states.length !== lights.size) throw Error("State reports unavailable.");
  const seen = new Set();
  for (const item of result.states) {
    const age = value => value === null || Number.isSafeInteger(value) && value >= 0;
    if (!item || !lights.has(item.light_id) || seen.has(item.light_id) || !["on","off","unavailable"].includes(item.state)
      || !["online","stale","unknown","unavailable","unchecked"].includes(item.availability)
      || !age(item.age_ms) || !(item.sample === null || Number.isFinite(item.sample))
      || (["online","stale"].includes(item.availability) && (!["on","off"].includes(item.state) || item.age_ms === null))
      || (item.availability === "online" && item.age_ms >= 45000)
      || (item.last_confirmed !== null && (!item.last_confirmed || !["on","off"].includes(item.last_confirmed.state) || !age(item.last_confirmed.age_ms)))) throw Error("State reports unavailable.");
    seen.add(item.light_id);
  }
  if (stateGeneration !== result.state_generation) {
    for (const light of lights.values()) {light.state="unchecked"; light.confirmedAt=null; light.lastConfirmed=null; light.reportSample=null; light.recoveryBarrier=null; light.needsRefresh=light.uncertain;}
    stateGeneration = result.state_generation;
  }
  for (const item of result.states) {
    const light = lights.get(item.light_id);
    if (operation?.lightId === item.light_id || item.sample !== null && light.reportSample !== null && item.sample < light.reportSample) continue;
    if (light.uncertain) {
      if (light.recoveryBarrier === null) light.recoveryBarrier = result.state_time;
      if (item.sample === null || item.sample <= light.recoveryBarrier || item.availability !== "online") continue;
      light.uncertain = false; light.recoveryBarrier = null;
    }
    light.reportSample = item.sample;
    light.state = item.state === "unavailable" && item.availability === "unchecked" ? "unchecked" : item.state === "unavailable" && item.availability === "unknown" ? "unknown" : item.state;
    light.confirmedAt = ["online","stale"].includes(item.availability) ? Date.now()-item.age_ms : null;
    light.lastConfirmed = item.last_confirmed && item.last_confirmed.age_ms !== null ? {state:item.last_confirmed.state,at:Date.now()-item.last_confirmed.age_ms} : null;
    light.needsRefresh = ["unknown","unavailable"].includes(item.availability);
  }
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
    automaticUpdates = result.automatic_updates === true;
    if (automaticUpdates) showStates(result);
    updates.setActive(automaticUpdates && !document.hidden);
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
  if (state === "unknown") {light.uncertain = true; light.recoveryBarrier = null;}
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
  updates.interrupt();
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
      operation = null; light.state = result.state; light.confirmedAt = Date.now(); light.lastConfirmed = {state:result.state,at:Date.now()}; light.needsRefresh = false;
      light.uncertain = false; light.recoveryBarrier = null;
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
  if (automaticUpdates && homeReady && !operation) { void updates.tick(); return; }
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
