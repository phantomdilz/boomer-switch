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
    if (!response.ok) throw new Error(result.error || "Access unavailable.");
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
  }[status] || "Access unavailable.";
  if (accessStatus.textContent !== text) accessStatus.textContent = text;
  if (status === "approved" && !controlsLoaded) {
    controlsLoaded = true;
    const script = document.createElement("script");
    script.src = "/app.js";
    document.head.append(script);
  }
}

async function checkAccess() {
  if (checking || submitting || document.hidden || ["anonymous", "invited", "verify"].includes(accessState)) return;
  checking = true;
  const startedVersion = accessVersion;
  try {
    const result = await accessRequest("/api/access");
    if (startedVersion === accessVersion) showAccess(result.status);
  }
  catch { if (startedVersion === accessVersion) showAccess("unavailable"); }
  finally { checking = false; }
}

async function submit(path, body) {
  if (submitting) return;
  accessVersion += 1;
  submitting = true;
  for (const button of document.querySelectorAll("form button")) button.disabled = true;
  try { showAccess((await accessRequest(path, body)).status); }
  catch (error) { accessStatus.textContent = error.message; }
  finally {
    submitting = false;
    for (const button of document.querySelectorAll("form button")) button.disabled = false;
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
document.addEventListener("visibilitychange", checkAccess);
checkAccess();
setInterval(checkAccess, 1500);
