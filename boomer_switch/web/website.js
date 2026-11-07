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
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("invitation")) sessionStorage.setItem("invitation", fragment.get("invitation"));
if (location.hash) history.replaceState(null, "", location.pathname);
async function request(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
    headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(10000)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Access unavailable.");
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
  if (!homeReady) clearControls("Use home internet and wait for its connection. This can take up to 60 seconds.");
}
async function submit(path, body) {
  if (busy) return;
  version += 1; busy = true;
  document.querySelectorAll("form button").forEach(button => button.disabled = true);
  try { show(await request(path, body)); }
  catch (error) { statusLine.textContent = error.message; }
  finally { busy = false; document.querySelectorAll("form button").forEach(button => button.disabled = false); }
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
    let result = await request("api/access");
    const invitation = sessionStorage.getItem("invitation");
    if (invitation) {
      sessionStorage.removeItem("invitation");
      result = await request("api/invite", {invitation});
    }
    if (started !== version) return;
    show(result);
    if (operation) await checkOperation();
    else if (homeReady && Date.now() - lastRead >= 5000) await startOperation("read");
  } catch {
    homeReady = false;
    connection.hidden = false;
    clearControls("Home access is unavailable. This page will check again.");
  } finally { checking = false; }
}
document.addEventListener("visibilitychange", () => {
  version += 1;
  homeReady = false;
  clearControls("Checking home access");
  lastAccess = 0;
  refresh();
});
refresh(); setInterval(refresh, 1000);
