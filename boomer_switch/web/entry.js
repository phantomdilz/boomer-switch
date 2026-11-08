"use strict";
// Keep the invitation until the server confirms it. Bootstrap never sends mail.
window.HouseLightsEntry = (() => {
  let invitation = null;
  let rejected = false;
  let ready = false;
  let stored = true;
  let revision = 0;
  try { invitation = sessionStorage.getItem("invitation"); } catch { stored = false; }
  function capture() {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const changed = fragment.has("invitation");
    if (changed) {
      const tokens = fragment.getAll("invitation");
      invitation = tokens.length === 1 && [...fragment.keys()].length === 1 && /^[A-Za-z0-9_-]{32,128}$/.test(tokens[0])
        ? tokens[0] : "";
      revision += 1; ready = false; rejected = false;
      try { sessionStorage.setItem("invitation", invitation); stored = true; }
      catch { stored = false; try { sessionStorage.removeItem("invitation"); } catch {} }
    }
    if (location.hash) history.replaceState(null, "", location.pathname);
    return changed;
  }
  capture();
  window.addEventListener("hashchange", () => {
    if (capture()) window.dispatchEvent(new Event("house-lights-entry"));
  });
  function clear(value) {
    if (invitation !== value) return;
    invitation = null;
    try { sessionStorage.removeItem("invitation"); } catch {}
  }
  function validate(result) {
    if (!result || !["anonymous", "invited", "verify", "pending", "approved", "denied"].includes(result.status)) {
      ready = false;
      throw new Error("Access confirmation unavailable.");
    }
    return result;
  }
  return {
    get ready() { return ready; },
    get canSubmit() { return ready && !rejected; },
    get recoveryHint() { return invitation !== null && !stored; },
    invalidate() { ready = false; },
    async bootstrap(request, access, accept) {
      const started = revision;
      let result = validate(await request(access));
      if (started !== revision) throw new Error("Invitation changed. Check access again.");
      if (invitation !== null) {
        const pending = invitation;
        try {
          result = await request(accept, {invitation: pending});
          if (started !== revision) throw new Error("Invitation changed. Check access again.");
          if (!["invited", "verify", "pending", "approved"].includes(result.status)) {
            throw new Error("Invitation confirmation unavailable.");
          }
          clear(pending);
          rejected = false;
        } catch (error) {
          if (started !== revision) throw error;
          if (error.code !== "invitation_unavailable") throw error;
          clear(pending);
          rejected = true;
          result = validate(await request(access));
        }
      }
      if (started !== revision) throw new Error("Invitation changed. Check access again.");
      ready = true;
      return rejected && !["approved", "pending", "denied"].includes(result.status)
        ? {...result, status: "invalid_invitation"} : result;
    },
  };
})();
