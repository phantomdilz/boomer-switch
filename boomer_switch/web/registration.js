"use strict";
const accessStatus = document.querySelector("#registration-status");
const details = document.querySelector("#details");
const verification = document.querySelector("#verification");
const controls = document.querySelector("#controls");
let checking = false;
let submitting = false;
let controlsLoaded = false;
let accessVersion = 0;
let accessState;
const entry = window.HouseLightsEntry;
const retryAccess = document.querySelector("#retry-access");
function enableForms() {
  document.querySelectorAll("form fieldset").forEach(fieldset => fieldset.disabled = submitting || !entry.canSubmit);
  document.querySelector("#entry-recovery").hidden = !entry.recoveryHint;
}

async function accessRequest(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch(path, {
      method: body ? "POST" : "GET", signal: controller.signal, cache: "no-store",
      headers: body ? {"Content-Type": "application/json"} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || "Access unavailable.");
      error.status = response.status; error.code = result.code;
      throw error;
    }
    return result;
  } finally { clearTimeout(timeout); }
}

function showAccess(status) {
  accessState = status;
  details.hidden = !["anonymous", "invited"].includes(status);
  verification.hidden = status !== "verify";
  controls.hidden = status !== "approved";
  const text = {
    anonymous: "Open an invitation, or verify an approved email again.",
    invited: "Enter your name and email.", verify: "Enter the code from your email.",
    pending: "Waiting for approval", approved: "Access approved", denied: "Access unavailable. Contact the owner.",
    invalid_invitation: "Invitation unavailable. Open a current invitation.",
  }[status] || "Access unavailable.";
  if (accessStatus.textContent !== text) accessStatus.textContent = text;
  if (status === "approved" && !controlsLoaded) {
    controlsLoaded = true;
    const script = document.createElement("script");
    script.src = "/app.js";
    document.head.append(script);
  }
}

async function checkAccess(force = false) {
  if (checking || submitting || document.hidden || (!force && entry.ready && ["anonymous", "invited", "verify", "invalid_invitation"].includes(accessState))) return;
  checking = true;
  const startedVersion = accessVersion;
  try {
    const result = await entry.bootstrap(accessRequest, "/api/access", "/api/invite");
    if (startedVersion === accessVersion) { showAccess(result.status); retryAccess.hidden = true; }
  }
  catch {
    if (startedVersion === accessVersion) {
      entry.invalidate();
      showAccess("unavailable");
      accessStatus.textContent = "Access unavailable. The page will check again.";
      retryAccess.hidden = false;
    }
  }
  finally { checking = false; enableForms(); }
}

async function submit(path, body) {
  if (submitting || !entry.canSubmit) return;
  accessVersion += 1;
  submitting = true;
  enableForms();
  try { showAccess((await accessRequest(path, body)).status); }
  catch (error) {
    accessStatus.textContent = error.message;
    if (!error.status || error.status >= 500) { entry.invalidate(); retryAccess.hidden = false; }
  }
  finally {
    submitting = false;
    enableForms();
  }
}

details.addEventListener("submit", event => {
  event.preventDefault();
  submit("/api/register", {name: document.querySelector("#name").value, email: document.querySelector("#email").value});
});
verification.addEventListener("submit", event => {
  event.preventDefault();
  submit("/api/verify", {code: document.querySelector("#code").value});
});
document.querySelector("#resend-code").addEventListener("click", () => submit("/api/resend", {}));
retryAccess.addEventListener("click", () => checkAccess(true));
window.addEventListener("house-lights-entry", () => { accessVersion += 1; enableForms(); checkAccess(true); });
document.addEventListener("visibilitychange", () => checkAccess());
checkAccess();
enableForms();
setInterval(checkAccess, 1500);
