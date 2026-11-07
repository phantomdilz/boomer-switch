# Private one-lamp backend milestone

This milestone supplies the home backend, website permission adapter, and harmless reachability check.
It does not supply the guest-side Wi-Fi proof producer, its HTTPS listener, or a browser sign-in handoff.
Do not enable real guest control before those parts pass their preflights under separate operator authority.
No deployment, certificate installation, live provider call, or physical switch test forms part of this implementation.

## First check: phone-to-spare reachability

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
No guest UI is published by this milestone.
Read-only targeted discovery treats `device_host` as a hint, then checks the exact stable device ID, type, and lack of child outlets.

All provider queries use zero SDK retries. Discovery and disconnect have separate bounded timeouts.
State comes from a fresh device read. A failed or ambiguous physical command returns Unavailable without automatic retry.

## Required guest-producer contract

A client certificate authenticates the producer, not the phone's Wi-Fi connection.
The future producer must bind exclusively to the selected physical guest wireless interface and address.
Before each forward, it must verify the pinned adapter and current approved Wi-Fi, directly attached client subnet, and absence of forwarding or overlay ingress.
It must deny service if the adapter roams, disappears, changes identity, or loses the expected address.
Another interface with the same address must not inherit service.
An IP address, a browser connection-type hint, or an untrusted request header is not enough proof.

The producer must keep its dedicated TLS client key exclusive and reject management paths.
It must discard client forwarding, deadline, and presence headers.
It sets `X-Pilot-Deadline` itself before the authenticated forward, normally 6 seconds ahead.
The home consumer rejects a lifetime longer than 8 seconds, an expired deadline, and queued commands whose deadline expires before dispatch.
The guest producer, home backend, and website host require compatible wall clocks.

The backend checks the accepted-request deadline and a fresh website grant again at the physical dispatch boundary.
Busy operations are rejected instead of queued. A command already accepted in good faith can finish within its bounded lifetime.

The spare uses Windows. Its trustworthy browser TLS certificate, exact wireless adapter, peer isolation behavior, and private home reachability still need preflight checks.
The guest listener and Windows proof implementation remain open for the next bounded work unit.

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

Stop the backend with Ctrl+C. Stop the future guest producer separately before changing its network or certificates.
The lamp keeps its current state after the backend stops.

Keep the journal after a timeout, transport loss, or process interruption. Do not delete it to bypass an unresolved operation.
After an approved restart, get a confirmed guarded state read before another command.
A nonempty journal with no known physical-device binding is refused and requires a controlled provenance review.

The pilot extra pins python-kasa 0.12.0 (GPL-3.0-or-later) and aiohttp 3.14.5 (Apache-2.0 AND MIT).
These are installed dependencies. Their source is not copied into this repository.
