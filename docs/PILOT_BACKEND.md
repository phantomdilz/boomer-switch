# Private one-lamp pilot

This implementation supplies the home backend, Windows guest entry point, website permission adapter, and harmless reachability check.
The guest entry point uses the production website's browser-bound sign-in handoff.
Do not enable real guest control before the separate website integration and private network preflights pass under operator authority.
No deployment, certificate installation, live provider call, or physical switch test forms part of this implementation.

## First check: phone-to-spare reachability

On Windows, download `guest-reachability.cmd` and double-click it. No Python installation is needed.
The check selects the sole connected Wi-Fi adapter and its private IPv4 address.
It prints the exact URL for the phone and closes after 2 minutes.
If Wi-Fi is missing or ambiguous, it shows a plain error and waits for a key.

It does not change firewall or network settings. Keep the window open to report the result.

The Python entry point remains available for operators who select the address explicitly:

On the spare computer, select its actual guest wireless address and an unused port.
Run this command only within the approved local reachability scope:

```text
python -m boomer_switch.guest_reachability --bind PRIVATE_GUEST_ADDRESS --port PORT --seconds 60
```

Open `http://PRIVATE_GUEST_ADDRESS:PORT/` from the phone on guest Wi-Fi.
The service returns one fixed harmless marker for at most 120 seconds, then closes.
It serves no files, accepts no identity, and has no device or permission functions.
Ctrl+C also stops it. It does not prove Wi-Fi identity or authorize the pilot.

If the phone cannot reach it while guest isolation stays enabled, the proposed direct guest-side entry path remains blocked.
Do not weaken router isolation to make this check pass.

## Home backend

Install the `pilot` extra in an isolated Python 3.11-or-later environment.
Copy `pilot.example.json` to an ignored private configuration file and replace every placeholder locally.
Use the existing private allowlist format, then select one explicit `light_id` in the pilot configuration.
The selected entry must describe one standalone lamp. No device is selected by discovery order or network address alone.

Relative configuration paths resolve from the configuration file's directory.
Optional `username` and `password` fields must both be present if the selected device needs them.

```text
python -m boomer_switch.pilot --config config.local.pilot.json --port PORT
```

The configured bind address must be one specific non-public local address.
The server requires TLS, a trusted dedicated guest-client CA, and the exact approved guest-client certificate fingerprint.
TLS handshakes have a 3-second socket timeout. No-client-certificate connections fail before the application runs.
Client headers such as `X-SSL-Client-Verify`, `X-Forwarded-For`, or a claimed Wi-Fi flag do not establish identity or presence.

The consumer reads the actual verified TLS peer. Forwarded and overlay identity headers are rejected.

Only these application routes exist:

- `GET /api/lights` returns the one selected lamp's display fields and confirmed state.
- `POST /api/lights/LIGHT_ID` accepts only `{"action":"on","operation_id":"CANONICAL_UUID"}` or the corresponding `off` action.

There is no owner page, fake sign-in, demo mailbox, registration endpoint, or filesystem route on this backend.
The guest browser page runs only on the separate Windows entry point.
Read-only targeted discovery treats `device_host` as a hint, then checks the exact stable device ID, type, and lack of child outlets.

All provider queries use zero SDK retries. Discovery and disconnect have separate bounded timeouts.
State comes from a fresh device read. A failed or ambiguous physical command returns Unavailable without automatic retry.

## Windows guest entry point

A client certificate authenticates the producer, not the phone's Wi-Fi connection.
The producer binds exclusively to the selected physical guest wireless address.
Before each forward, it checks the pinned adapter GUID and index, SSID, BSSID, private address, subnet, and direct neighbor.
It requires a physical active adapter and an on-link route with no competing specific route.
All IPv4 interfaces must disable forwarding and weak-host send and receive.
Peers that match a configured next-hop router or default gateway on the selected adapter are denied.

The native WLAN query runs last. It requires an infrastructure connection with WPA2 or WPA3 authentication.

It denies service if the adapter roams, disappears, changes identity, or loses the expected address.
Another interface with the same address must not inherit service.
An IP address, a browser connection-type hint, or an untrusted request header is not enough proof.

The producer must keep its dedicated TLS client key exclusive and reject management paths.
It rejects client forwarding, deadline, authorization, and presence headers.
It sets `X-Pilot-Deadline` itself before the authenticated forward, 6 seconds after the proof query starts.
The proof query must finish within 4 seconds. No device request waits in a queue.

If a native WLAN call stalls, the request fails within 4 seconds and no further proof worker starts until it returns.
Its late result cannot authorize a later request. A stuck native service requires an operator check or process restart.

The home consumer rejects a lifetime longer than 8 seconds, an expired deadline, and queued commands whose deadline expires before dispatch.
The guest producer, home backend, and website host require compatible wall clocks.

The backend checks the accepted-request deadline and a fresh website grant again at the physical dispatch boundary.
Busy operations are rejected instead of queued. A command already accepted in good faith can finish within its bounded lifetime.

Copy `guest.example.json` to `config.local.guest.json` on the Windows spare and replace each placeholder privately.
Use an approved browser-trusted certificate for the exact `origin`, with DNS that resolves to the selected Wi-Fi address.
Keep the dedicated home client key and website service key readable only by the service account.
The home service must trust only this dedicated client certificate and its pinned fingerprint.

```text
python -m boomer_switch.guest_proxy --config config.local.guest.json
```

The listener port comes from `origin`. No wildcard or overlay listener is created.
The page shows one lamp, explicit On and Off buttons, confirmed state, and a 10-second read interval while visible.
Each click has a new operation ID. Commands have no automatic retry.

### Real website handoff

Configure the production website's fixed `home_entry` as this origin's `/auth/start` and its fixed callback as `/auth/callback`.
The entry point redirects to `website_guest_url#challenge=...&state=...` with a browser-bound pending cookie.
The real website requires verified inbox ownership and current owner approval before it issues its one-use code.
The callback checks the pending cookie and state, then posts `{code,challenge,request_id}` to the explicit HTTPS `website_exchange_url`.
The authenticated response must contain `{session,expires,request_id}`, a HOME-scoped 64-character hex token, and an expiry within 24 hours.

The entry point sets a Secure, HttpOnly, SameSite=Strict guest cookie and removes the code from the browser URL.
It does not copy a website cookie or accept a manually supplied pilot credential.

Pending handoffs expire after 120 seconds and do not survive a process restart.
Completed browser sessions still depend on the website's persisted map and a fresh grant for every read and command.
The website adapter is a separate component. These synthetic tests do not establish production hosting or live mail delivery.

### Required private preflights

Verify phone-to-spare reachability, browser certificate trust, exact WLAN identity, native-query permissions, and private home mTLS reachability.
Windows can deny WLAN information when location access is unavailable. The service fails closed and does not change that setting.
Exclude proxy ARP, untrusted relays, forwarding, and VPN paths that make remote clients appear directly attached.
The producer proves its Windows attachment and a direct local peer. It cannot detect a separate trusted-looking application relay elsewhere on the LAN.

The approved deployment profile must exclude such relays. IP addresses alone do not prove phone presence.

The real website's sign-in, mail, fixed callback, authorization map, HTTPS grants, and clocks require integration checks before any physical command.
No certificate provisioning, firewall rule, router change, deployment, or live phone/device test is included here.
If any required proof is missing, access remains unavailable.

## Website permission endpoint

Mount `create_website_grant_application` under an independently approved private website prefix:

```python
from pathlib import Path
from boomer_switch.website_grants import create_website_grant_application

application = create_website_grant_application(
    Path("PRIVATE_AUTHORIZATION_DATABASE"),
    service_key="PRIVATE_RANDOM_SERVICE_KEY_AT_LEAST_32_CHARACTERS",
)
```

The only route inside that mount is `POST /grant` over server-verified HTTPS with the private home-service bearer key.
Keep the key out of browser code, URLs, request logs, and committed files.
The database must be the canonical persisted website authorization store, not demonstration state.
The endpoint opens it read-only and never initializes invitations, fake mail, sessions, or approvals.

It resolves a home-scoped opaque session through `home_sessions(id, website_session, expires)` to the canonical website session and persisted guest identity.
It checks home-session expiry, website-session expiry, current approval, inbox proof, and access expiry.
An ordinary website cookie does not substitute for a home-scoped session.
It does not accept an email address as proof of identity and returns no name, email, credential, or raw session.

The home client validates HTTPS certificates and hostnames, refuses redirects and proxy fallback, bounds replies to 4096 bytes, and uses a 2-second total timeout.
The grant window also uses website time. An ahead-of-home website clock can cause denial if its skew exceeds transport time.
Check clock alignment and the complete grant round trip during the approved preflight.

Each decision is bound to the session hash and operation ID. Positive decisions are not cached.
The initial check precedes device discovery. The final check occurs after discovery and before the effect or state projection.

The real website's verified-session issuer and its delivery at the guest origin remain integration blockers.
The owner selected the production website's real registration and mail path first.
The backend does not create credentials, send mail, or approve guests.

## Stop, duplicates, and reconciliation

Use one normal home process per operation journal. The CLI holds an exclusive process lock.
Each journal is bound to one physical device ID. A different selected device cannot reuse it to clear an earlier unknown operation.
Operation IDs are reserved before work and marked dispatched before a possible physical effect.
The backend never repeats dispatch for the same ID.

Unknown or interrupted effects also block new IDs for that lamp across restart.
Only an authenticated, authorized, confirmed device read clears that unresolved barrier.
Pre-dispatch permission denial does not become an unknown physical effect.

Stop both processes with Ctrl+C before changing their network or certificates.
The lamp keeps its current state after the backend stops.

Keep the journal after a timeout, transport loss, or process interruption. Do not delete it to bypass an unresolved operation.
After an approved restart, get a confirmed guarded state read before another command.
A nonempty journal with no known physical-device binding is refused and requires a controlled provenance review.

The pilot extra pins python-kasa 0.12.0 (GPL-3.0-or-later) and aiohttp 3.14.5 (Apache-2.0 AND MIT).
These are installed dependencies. Their source is not copied into this repository.
