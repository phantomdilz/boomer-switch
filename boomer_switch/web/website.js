"use strict";
const statusLine = document.querySelector("#status");
const details = document.querySelector("#details");
const verification = document.querySelector("#verification");
const connect = document.querySelector("#connect");
let csrf = "";
let busy = false;
let approved = false;
let homeEntry = null;
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("invitation")) sessionStorage.setItem("invitation", fragment.get("invitation"));
if (location.hash) history.replaceState(null, "", location.pathname);
if (fragment.has("challenge") && fragment.has("state")) {
  sessionStorage.setItem("homeChallenge", fragment.get("challenge"));
  sessionStorage.setItem("homeState", fragment.get("state"));
  history.replaceState(null, "", location.pathname);
}
async function request(path, body) {
  const response = await fetch(path, {method: body ? "POST" : "GET", cache: "no-store",
    headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
    body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(30000)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Access unavailable.");
  if (result.csrf) csrf = result.csrf;
  return result;
}
function show(result) {
  approved = result.status === "approved";
  homeEntry = result.home_entry || null;
  details.hidden = !["anonymous", "invited"].includes(result.status);
  verification.hidden = result.status !== "verify";
  connect.hidden = !approved || !(homeEntry || sessionStorage.getItem("homeChallenge"));
  statusLine.textContent = {anonymous: "Open an invitation, or verify an approved email again.", invited: "Enter your name and email.", verify: "Enter the code from your email.", pending: "Waiting for approval", approved: "Access approved. Open the home controls.", denied: "Access unavailable. Contact the owner."}[result.status] || "Access unavailable.";
}
async function submit(path, body) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach(button => button.disabled = true);
  try { show(await request(path, body)); }
  catch (error) { statusLine.textContent = error.message; }
  finally { busy = false; document.querySelectorAll("button").forEach(button => button.disabled = false); }
}
details.addEventListener("submit", event => {event.preventDefault(); submit("api/register", {name: document.querySelector("#name").value, email: document.querySelector("#email").value});});
verification.addEventListener("submit", event => {event.preventDefault(); submit("api/verify", {code: document.querySelector("#code").value});});
document.querySelector("#resend").addEventListener("click", () => submit("api/resend", {}));
connect.addEventListener("click", async () => {
  if (busy || !approved) return;
  busy = true;
  if (!sessionStorage.getItem("homeChallenge")) { location.replace(homeEntry); return; }
  try {
    const result = await request("api/handoff", {challenge: sessionStorage.getItem("homeChallenge"), state: sessionStorage.getItem("homeState")});
    sessionStorage.removeItem("homeChallenge"); sessionStorage.removeItem("homeState");
    location.replace(result.redirect);
  } catch (error) { statusLine.textContent = error.message; busy = false; }
});
async function refresh() {
  if (busy || document.hidden) return;
  try {
    let result = await request("api/access");
    const invitation = sessionStorage.getItem("invitation");
    if (invitation) {
      sessionStorage.removeItem("invitation");
      result = await request("api/invite", {invitation});
    }
    show(result);
  } catch { statusLine.textContent = "Access unavailable. Refresh this page."; }
}
refresh(); setInterval(refresh, 3000);
