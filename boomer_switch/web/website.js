"use strict";
const statusLine = document.querySelector("#status");
const details = document.querySelector("#details");
const verification = document.querySelector("#verification");
const controls = document.querySelector("#controls");
const connection = document.querySelector("#connection");
const stateLine = document.querySelector("#light-state");
const on = document.querySelector("#light-on");
const off = document.querySelector("#light-off");
const card = document.querySelector("#light-card");
const lightName = document.querySelector("#light-name");
const roomName = document.querySelector("#light-room");
const refreshButton = document.querySelector("#refresh-light");
let csrf = "";
let busy = false;
let checking = false;
let approved = false;
let homeReady = false;
let operation = null;
let version = 0;
let lastRead = 0;
let lastAccess = 0;
const entry = window.HouseLightsEntry;
const retryAccess = document.querySelector("#retry-access");
function enableForms() {
  document.querySelectorAll("form fieldset").forEach(fieldset => fieldset.disabled = busy || !entry.canSubmit);
  document.querySelector("#entry-recovery").hidden = !entry.recoveryHint;
}
async function request(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
    headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(10000)});
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "Access unavailable.");
    error.status = response.status; error.code = result.code;
    throw error;
  }
  if (result.csrf) csrf = result.csrf;
  return result;
}
function unavailable(text) {
  stateLine.textContent = "Unavailable";
  on.disabled = true; off.disabled = true;
  connection.textContent = text;
}
function clearControls(text) {
  operation = null;
  controls.hidden = true;
  lightName.textContent = ""; roomName.textContent = "";
  delete card.dataset.lightId;
  refreshButton.disabled = true;
  unavailable(text);
}
function show(result) {
  approved = result.status === "approved";
  homeReady = approved && result.home_ready === true;
  details.hidden = !["anonymous", "invited"].includes(result.status);
  verification.hidden = result.status !== "verify";
  controls.hidden = !homeReady;
  connection.hidden = !approved;
  if (homeReady && result.light) {
    card.dataset.lightId = result.light.light_id;
    lightName.textContent = result.light.name; roomName.textContent = result.light.room;
  }
  refreshButton.disabled = !homeReady || operation !== null;
  statusLine.textContent = {anonymous: "Open an invitation, or verify an approved email again.", invited: "Enter your name and email.", verify: "Enter the code from your email.", pending: "Waiting for approval", approved: "Access approved", denied: "Access unavailable. Contact the owner."}[result.status] || "Access unavailable.";
  if (result.status === "invalid_invitation") statusLine.textContent = "Invitation unavailable. Open a current invitation.";
  if (!homeReady) clearControls("Use home internet and wait for its connection. This can take up to 60 seconds.");
}
async function submit(path, body) {
  if (busy || !entry.canSubmit) return;
  version += 1; busy = true;
  enableForms();
  try { show(await request(path, body)); }
  catch (error) {
    statusLine.textContent = error.message;
    if (!error.status || error.status >= 500) { entry.invalidate(); retryAccess.hidden = false; }
  }
  finally { busy = false; enableForms(); }
}
details.addEventListener("submit", event => {event.preventDefault(); submit("api/register", {name: document.querySelector("#name").value, email: document.querySelector("#email").value});});
verification.addEventListener("submit", event => {event.preventDefault(); submit("api/verify", {code: document.querySelector("#code").value});});
document.querySelector("#resend").addEventListener("click", () => submit("api/resend", {}));
async function startOperation(action) {
  if (busy || operation || !homeReady) return;
  busy = true;
  const id = crypto.randomUUID();
  operation = {id, until: Date.now() + 8000};
  unavailable(action === "read" ? "Checking current state" : "Waiting for command result");
  refreshButton.disabled = true;
  try {
    await request("api/lights", {operation_id: id, action});
    lastRead = Date.now();
  } catch (error) {
    operation = null;
    homeReady = false;
    clearControls(error.message + " Refresh state before another command.");
  } finally { busy = false; }
}
async function checkOperation() {
  if (!operation || busy || !homeReady) return;
  const current = operation;
  try {
    const result = await request("api/operations/" + current.id);
    if (operation !== current) return;
    if (result.status === "confirmed" && ["on", "off"].includes(result.state)) {
      operation = null;
      stateLine.textContent = result.state === "on" ? "On" : "Off";
      connection.textContent = "Confirmed state from the home light";
      on.disabled = false; off.disabled = false; refreshButton.disabled = false;
    } else if (["unknown", "denied", "not_started"].includes(result.status) || Date.now() >= current.until) {
      operation = null;
      unavailable("Result unavailable. Refresh state before another command.");
      refreshButton.disabled = false;
    }
  } catch (error) {
    operation = null;
    homeReady = false;
    clearControls(error.message + " Refresh state before another command.");
  }
}
on.addEventListener("click", () => startOperation("on"));
off.addEventListener("click", () => startOperation("off"));
refreshButton.addEventListener("click", () => startOperation("read"));
async function refresh() {
  if (busy || checking || document.hidden) return;
  checking = true;
  const started = version;
  try {
    if (Date.now() - lastAccess < 3000) {
      if (operation) await checkOperation();
      return;
    }
    lastAccess = Date.now();
    const result = await entry.bootstrap(request, "api/access", "api/invite");
    if (started !== version) return;
    show(result);
    retryAccess.hidden = true;
    if (operation) await checkOperation();
    else if (homeReady && Date.now() - lastRead >= 5000) await startOperation("read");
  } catch {
    if (started !== version) return;
    entry.invalidate();
    retryAccess.hidden = false;
    statusLine.textContent = "Access unavailable. The page will check again.";
    homeReady = false;
    connection.hidden = false;
    clearControls("Home access is unavailable. This page will check again.");
  } finally { checking = false; enableForms(); }
}
document.addEventListener("visibilitychange", () => {
  version += 1;
  homeReady = false;
  clearControls("Checking home access");
  lastAccess = 0;
  refresh();
});
retryAccess.addEventListener("click", () => { lastAccess = 0; refresh(); });
window.addEventListener("house-lights-entry", () => { version += 1; lastAccess = 0; enableForms(); refresh(); });
refresh(); setInterval(refresh, 1000);
enableForms();
