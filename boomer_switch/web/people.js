"use strict";
const peopleStatus = document.querySelector("#people-status");
const pendingPeople = document.querySelector("#pending-people");
const approvedPeople = document.querySelector("#approved-people");
let people = [];
let csrf = "";
let changing = false;
document.querySelector("#time-zone").textContent = `Time zone: ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;

async function peopleRequest(body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3000);
  try {
    const response = await fetch("api/people", {
      method: body ? "POST" : "GET", cache: "no-store", signal: controller.signal,
      headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "People unavailable.");
    csrf = result.csrf;
    return result.people;
  } finally { clearTimeout(timeout); }
}

function localDate(timestamp) {
  const date = new Date(timestamp * 1000 - 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function button(text, action, callback) {
  const result = document.createElement("button");
  result.type = "button";
  result.textContent = text;
  result.dataset.action = action;
  result.addEventListener("click", callback);
  return result;
}

function render() {
  pendingPeople.replaceChildren();
  approvedPeople.replaceChildren();
  for (const person of people) {
    if (!["pending", "approved"].includes(person.status)) continue;
    if (person.status === "pending" && !person.email_verified) continue;
    const row = document.createElement("article");
    row.className = "light person";
    row.dataset.email = person.email;
    row.dataset.status = person.status;
    const name = document.createElement("h3");
    name.textContent = person.name || "Awaiting email proof";
    const email = document.createElement("p");
    email.className = "person-email";
    email.textContent = person.email;
    const state = document.createElement("p");
    state.dataset.access = "";
    state.textContent = person.status === "pending" ? "Pending" : person.expires !== null && person.expires <= Date.now() / 1000 ? "Expired" : person.email_verified ? "Approved" : "Awaiting email proof";
    row.append(name, email, state);
    const actions = document.createElement("div");
    actions.className = "actions";
    if (person.status === "pending") {
      actions.append(button("Approve", "approve", () => change(person, "approve")), button("Deny", "deny", () => change(person, "deny")));
      row.append(actions);
      pendingPeople.append(row);
    } else {
      const label = document.createElement("label");
      label.textContent = "Last day of access";
      const date = document.createElement("input");
      date.type = "date";
      date.min = "0001-01-01";
      date.max = "9999-12-30";
      date.dataset.endDate = "";
      date.setAttribute("aria-describedby", "date-help time-zone");
      date.value = person.expires === null ? "" : localDate(person.expires);
      label.append(date);
      const deadline = document.createElement("p");
      deadline.textContent = person.expires === null ? "No end date" : `Access ends: ${new Date(person.expires * 1000).toLocaleString()}`;
      actions.append(button("Save end date", "save-expiry", () => {
        if (!date.value || !date.checkValidity()) { peopleStatus.textContent = "Choose a valid last day of access."; return; }
        const [year, month, day] = date.value.split("-").map(Number);
        const deadline = new Date(0);
        deadline.setFullYear(year, month - 1, day + 1);
        deadline.setHours(0, 0, 0, 0);
        const seconds = deadline.getTime() / 1000;
        if (!Number.isFinite(seconds) || seconds < -62135596800 || seconds > 253402300799) {
          peopleStatus.textContent = "Choose a valid last day of access.";
          return;
        }
        change(person, "expiry", seconds);
      }), button("Clear end date", "clear-expiry", () => change(person, "expiry", null)));
      row.append(label, deadline, actions, button("Remove access", "remove", () => change(person, "remove")));
      approvedPeople.append(row);
    }
  }
  if (!pendingPeople.children.length) pendingPeople.textContent = "No pending requests.";
  if (!approvedPeople.children.length) approvedPeople.textContent = "No approved people.";
}

async function change(person, action, expires) {
  if (changing) return;
  changing = true;
  for (const control of document.querySelectorAll("button")) control.disabled = true;
  const body = {action, email: person.email, revision: person.revision};
  if (action === "expiry") body.expires = expires;
  try { people = await peopleRequest(body); render(); peopleStatus.textContent = "People updated."; }
  catch (error) {
    peopleStatus.textContent = error.message;
    try { people = await peopleRequest(); render(); } catch { pendingPeople.replaceChildren(); approvedPeople.replaceChildren(); }
  } finally {
    changing = false;
    for (const control of document.querySelectorAll("button")) control.disabled = false;
  }
}

document.querySelector("#add-person-form").addEventListener("submit", event => {
  event.preventDefault();
  const email = document.querySelector("#add-email").value.toLowerCase();
  const current = people.find(person => person.email === email);
  change({email, revision: current ? current.revision : null}, "preapprove");
});
document.querySelector("#refresh-people").addEventListener("click", async () => {
  if (changing) return;
  try { people = await peopleRequest(); render(); peopleStatus.textContent = "People ready."; }
  catch { pendingPeople.replaceChildren(); approvedPeople.replaceChildren(); peopleStatus.textContent = "People unavailable. Sign in as the owner and refresh."; }
});
peopleRequest().then(result => { people = result; render(); peopleStatus.textContent = "People ready.";
  invitationRequest().catch(() => { invitationSection.hidden = true; }); })
  .catch(() => { peopleStatus.textContent = "People unavailable. Sign in as the owner and refresh."; });

let invitationRevision = null;
let replacingInvitation = false;
const invitationSection = document.querySelector("#invitation");
const invitationStatus = document.querySelector("#invitation-status");
const replacementWarning = document.querySelector("#replace-warning");
async function invitationRequest(body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch("api/invitation", {method: body ? "POST" : "GET", cache: "no-store", signal: controller.signal,
      headers: body ? {"Content-Type": "application/json", "X-CSRF-Token": csrf} : {},
      body: body ? JSON.stringify(body) : undefined});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Invitation unavailable.");
    if (!/^[a-f0-9]{64}$/.test(result.revision) || typeof result.csrf !== "string") throw new Error("Invitation unavailable.");
    csrf = result.csrf;
    invitationRevision = result.revision;
    document.querySelector("#invitation-pdf").hidden = result.pdf_available !== true;
    invitationSection.hidden = false;
  } finally { clearTimeout(timeout); }
}
document.querySelector("#replace-invitation").addEventListener("click", () => {
  if (replacingInvitation || changing || !invitationRevision) return;
  replacementWarning.hidden = false;
  document.querySelector("#cancel-replace").focus();
});
document.querySelector("#cancel-replace").addEventListener("click", () => { replacementWarning.hidden = true; });
document.querySelector("#confirm-replace").addEventListener("click", async () => {
  if (replacingInvitation || changing || !invitationRevision || replacementWarning.hidden) return;
  replacingInvitation = true;
  const revision = invitationRevision;
  replacementWarning.hidden = true;
  document.querySelector("#replace-invitation").disabled = true;
  try {
    await invitationRequest({revision, confirmation: "replace"});
    invitationStatus.textContent = "Invitation replaced. Print the new card.";
  } catch {
    invitationRevision = null;
    invitationStatus.textContent = "The result is unconfirmed. Reload People and open the current card before you replace it again.";
  } finally {
    replacingInvitation = false;
    document.querySelector("#replace-invitation").disabled = invitationRevision === null;
  }
});
