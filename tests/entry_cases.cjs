const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const source = fs.readFileSync(process.argv[2], "utf8");
const token = "x".repeat(43);
function fixture(hash = "#invitation=" + token, blocked = false, previous = null) {
  const storage = new Map(previous === null ? [] : [["invitation", previous]]);
  const listeners = new Map();
  const location = {hash, pathname: "/guest"};
  const window = {addEventListener: (name, callback) => listeners.set(name, callback), dispatchEvent: () => {}};
  const sessionStorage = {
    getItem: key => { if (blocked) throw Error("storage blocked"); return storage.get(key) ?? null; },
    setItem: (key, value) => { if (blocked) throw Error("storage blocked"); storage.set(key, value); },
    removeItem: key => { if (blocked) throw Error("storage blocked"); storage.delete(key); },
  };
  vm.runInNewContext(source, {window, location, sessionStorage, URLSearchParams, Event,
    history: {replaceState: () => { location.hash = ""; }}});
  return {entry: window.HouseLightsEntry, storage, location, change(hash) {
    location.hash = hash; listeners.get("hashchange")();
  }};
}
test("lost bootstrap and acceptance responses retain invitation without mail retry", async () => {
  const f = fixture();
  assert.equal(f.location.hash, "");
  await assert.rejects(f.entry.bootstrap(async () => { throw Error("offline"); }, "access", "accept"));
  assert.equal(f.storage.get("invitation"), token);
  const calls = [];
  await assert.rejects(f.entry.bootstrap(async (path, body) => {
    calls.push([path, body]); if (path === "accept") throw Error("lost response"); return {status: "anonymous"};
  }, "access", "accept"));
  assert.equal(f.entry.canSubmit, false);
  const accepted = await f.entry.bootstrap(async (path, body) => {
    if (path === "accept") assert.equal(body.invitation, token);
    return {status: path === "accept" ? "invited" : "anonymous"};
  }, "access", "accept");
  assert.equal(accepted.status, "invited");
  assert.equal(f.storage.has("invitation"), false);
  assert.equal(f.entry.canSubmit, true);
  assert.deepEqual(calls.map(item => item[0]), ["access", "accept"]);
});
test("unexpected success cannot acknowledge the invitation", async () => {
  for (const status of [undefined, "anonymous", "unknown", "denied"]) {
    const f = fixture();
    await assert.rejects(f.entry.bootstrap(async path => path === "access" ? {status: "anonymous"} : {status}, "access", "accept"));
    assert.equal(f.storage.get("invitation"), token);
    assert.equal(f.entry.canSubmit, false);
  }
});
test("terminal invalid attempts clear storage but preserve approved bookmarks", async () => {
  for (const status of ["anonymous", "approved"]) {
    const f = fixture();
    const result = await f.entry.bootstrap(async path => {
      if (path === "accept") { const error = Error("invalid"); error.code = "invitation_unavailable"; throw error; }
      return {status};
    }, "access", "accept");
    assert.equal(result.status, status === "approved" ? "approved" : "invalid_invitation");
    assert.equal(f.storage.has("invitation"), false);
    assert.equal(f.entry.canSubmit, false);
  }
});
test("unrelated anchors keep an existing pending invitation", async () => {
  const f = fixture("#controls", false, token);
  await f.entry.bootstrap(async (path, body) => {
    if (path === "accept") assert.equal(body.invitation, token);
    return {status: "invited"};
  }, "access", "accept");
  assert.equal(f.entry.canSubmit, true);
  let finish;
  const checking = f.entry.bootstrap(() => new Promise(resolve => { finish = resolve; }), "access", "accept");
  assert.equal(f.entry.canSubmit, true); // A routine status check does not disable an acknowledged form.
  finish({status: "invited"}); await checking;
});
test("malformed invitation fragments cannot reuse an older retained grant", async () => {
  for (const hash of ["#invitation=", "#invitation=" + token + "&invitation=" + token, "#invitation=bad&other=value"]) {
    const f = fixture(hash, false, token);
    await f.entry.bootstrap(async (path, body) => {
      if (path === "accept") { assert.equal(body.invitation, ""); const error = Error("invalid"); error.code = "invitation_unavailable"; throw error; }
      return {status: "anonymous"};
    }, "access", "accept");
    assert.equal(f.entry.canSubmit, false);
    assert.equal(f.storage.size, 0);
  }
});
test("storage failure has explicit recovery guidance and keeps memory retry", async () => {
  const f = fixture(undefined, true);
  assert.equal(f.entry.recoveryHint, true);
  assert.equal(f.location.hash, "");
  await assert.rejects(f.entry.bootstrap(async () => { throw Error("offline"); }, "access", "accept"));
  await f.entry.bootstrap(async () => ({status: "invited"}), "access", "accept");
  assert.equal(f.entry.recoveryHint, false);
  assert.equal(f.entry.canSubmit, true);
});
test("a changed invitation cannot be acknowledged by an older in-flight response", async () => {
  const f = fixture();
  let finish;
  const pending = f.entry.bootstrap(async path => path === "access" ? {status: "anonymous"} : new Promise(resolve => { finish = resolve; }), "access", "accept");
  await new Promise(resolve => setImmediate(resolve));
  const newer = "y".repeat(43);
  f.change("#invitation=" + newer);
  finish({status: "invited"});
  await assert.rejects(pending);
  assert.equal(f.entry.canSubmit, false);
  assert.equal(f.storage.get("invitation"), newer);
});
