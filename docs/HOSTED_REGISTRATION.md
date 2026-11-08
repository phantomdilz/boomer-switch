# Hosted registration and light controls

The external website is the guest's only origin.
Its invitation, registration, approval wait, controls, and bookmark use the same HTTPS namespace.
The owner uses the existing private People page and sign-in.
The home agent makes outbound requests. Guests need no home app, callback, VPN, or Wi-Fi configuration.
No inbound home service, tunnel, or port forwarding forms part of this route.

Every state request and command requires a current verified approval and the current home internet source.
The website compares its direct IPv4 peer with the source of a fresh authenticated home poll.
This proves shared internet egress. It does not distinguish Wi-Fi, wired clients, or a VPN through home.
The operator accepted that boundary. A cellular or other remote source fails closed.

## IPv4 and trusted hosting

This pilot uses an IPv4-only application origin.
Configure and verify its IPv4 DNS route for both the browser and home agent before a live test.
An AAAA record, IPv6 HTTPS/SVCB hint, or alternate IPv6 route must not bypass this requirement.

The home client uses IPv4. The website rejects every IPv6 source, including IPv4-mapped IPv6.
It never infers a shared IPv6 prefix from one address. There is no automatic address-family fallback.

A later IPv6 design needs its own verified source contract.

Use a direct host connection for this namespace.
The PHP bridge reads server `REMOTE_ADDR`, not a browser field or forwarded header.
Its private `direct_source` setting must remain false until the operator verifies the server's peer semantics.
A CDN, reverse proxy, or server module that rewrites the peer makes this topology unsupported.

Forwarded-source headers disable device authority, even when the remaining request authenticates.
Do not copy `X-Forwarded-For`, `Forwarded`, or another client header into the peer field.
An unsupported host can serve registration, but it cannot authorize light state or commands.

The IPv4-only origin must also support the owner's existing sign-in on that origin.
A new hostname does not automatically share a host-only sign-in cookie.
Keep the existing website root and login unchanged until a scoped installation plan resolves that integration.

## Passenger and private Python

Use `boomer_switch.website_passenger.load_application` for the persistent Passenger route.
It composes the same production factory, canonical store, mail owner, and home rendezvous as the CGI route.
It adds hosting proof, current private owner validation, and explicit rollout switches.
It starts no network listener and sends no mail at startup.

Install the wheel with the `website` extra in a private Python 3.11 or later environment with SQLite and SSL.
The wheel includes startup and configuration examples under `share/boomer-switch/hosting`.
Copy `hosting/passenger_wsgi.py` to the private Passenger application root.
Copy the JSON example there as `config.local.passenger.json`, restrict its permissions, and replace every placeholder locally.
Keep the application root, validator, configuration, and canonical state outside the document root.
Configure Passenger's WSGI application root, startup file, private Python executable, and the dedicated base URI.
The base URI must equal JSON `mount`; its trusted WSGI `SCRIPT_NAME` must match exactly.
The server must supply HTTPS scheme and the exact configured host, and enforce a 25-second total request deadline and a 4096-byte body limit.

Apache must overwrite the peer header on every request in this namespace:

```apache
RequestHeader set X-HL-Apache-Peer "expr=%{CONN_REMOTE_ADDR}"
```

Use the physical connection variable `CONN_REMOTE_ADDR`, not the possibly rewritten `REMOTE_ADDR` expression.
Verify this overwrite with normal and forged-header requests before setting `apache_peer_verified` true.
The adapter requires its value to equal Passenger `REMOTE_ADDR` on each private or pilot request.
Never substitute a client-supplied header, forwarded address, or a Boolean from a request.
An unsupported proxy topology stays closed.

The private `owner_validator_command` is an absolute array command for the existing PHP owner-session validator.
It reads one raw cookie value from stdin (at most 1024 bytes), emits exactly `MATCH` on success, and exits zero.
Every other output, error, timeout, or absent cookie denies access.
The validator must run in the foreground without background children and must not log the cookie.
Its existing private configuration retains the site's owner secret; do not copy that secret into this component.
`owner_cookie` selects the existing cookie name. Arbitrary identity headers and inherited `REMOTE_USER` are ignored.
The adapter revalidates the owner at dispatch after reading the body; People retains its CSRF and revision checks.

Keep the existing website login and access rules outside this namespace unchanged.
Route the component's public guest page, `entry.js`, `website.js`, `style.css`, invitation links, registration APIs, and authenticated `/home/*` APIs to this application.
Keep `/people`, `/people.js`, and `/api/people` behind its private validator.
Do not retain the read-only stage's GET-only route allowlist when promoting registration.
Publish the app, page, scripts, and scoped Apache asset rule together as one new release.
Do not modify a staged immutable release in place.

Replace the stage's GET-only Apache login exceptions with this exact component route/method set.
Apply each path relative to the configured mount, with an end anchor; do not exempt the website root or the whole namespace from login.

| Method | Component path | Apache login rule | Application check |
| --- | --- | --- | --- |
| GET | `/`, `/guest`, `/entry.js`, `/website.js`, `/style.css` | Public | Exact HTTPS host and mount |
| GET | `/s/<alias>`, `/invite/<token>` | Public | Stateless invitation redirect; registration validates the invitation |
| GET | `/api/access` | Public | Canonical guest session; private light labels require current permission and source |
| POST | `/api/invite`, `/api/register`, `/api/resend`, `/api/verify` | Public | Origin, session CSRF, invitation, limits, and rollout switches |
| POST | `/api/lights` | Public | Origin, CSRF, current approval, fresh matching source, and pilot switch |
| GET | `/api/operations/<uuid>` | Public | Current guest approval, matching source, and operation ownership |
| POST | `/home/poll`, `/home/authorize`, `/home/result` | Public | Private machine Bearer key and canonical source/operation policy |
| GET | `/people`, `/people.js`, `/api/people` | Existing owner login required | Current private owner validator and bound owner session |
| POST | `/api/people` | Existing owner login required | Current validator after body, owner CSRF, revision, and People write switch |

Limit alias, token, and UUID suffixes to one path segment. Other methods and paths get no login exception.
Do not add exceptions for demo mailbox, operator, exchange, or retired grant routes.
The existing owner sign-in must remain usable on this exact origin.
Verify both an anonymous private request and a signed-in People request through the actual Apache gate before promotion.

Suppress request targets, query strings, Referer, cookies, and authorization headers in access/error diagnostics for this namespace.
Retained legacy `/invite/<token>` and typed `/s/<alias>` requests contain invitation secrets in their path.
Use scoped logs with method, status, and duration only, or exclude these component requests from target-bearing logs.
Apply the same rule to upstream hosting logs, traces, and error reports before publishing a card.
New full invitation fragments never enter the HTTP request target; keep `Referrer-Policy: no-referrer`.

All `passenger` switches in the example are false. Replace the private JSON atomically to change switches.
The application rereads them for each request, after body reading, and immediately before a mail attempt.
Fixed settings (paths, origin, credentials, validator) require an approved Passenger restart.

1. Verify read-only guest assets and private People with all effect switches off.
2. After mail authority and transport checks, enable `registration_enabled` and `mail_enabled` together.
   Enable `people_write_enabled` only when owner changes are approved.
3. Enable `pilot_enabled` only after the separate home-agent, source, and hardware preflight.
   A paused pilot reports no home readiness or light labels and does not create browser demand.

Turn an effect switch off to pause it. Existing approvals and bookmarks remain in canonical state.
An authenticated `/home/result` can still acknowledge an already accepted result while the pilot is paused.
No paused route can grant a new device operation.
A mail pause before the provider attempt leaves the unsent row recoverable through normal bounded delivery after resume.
An actual uncertain provider attempt remains `unknown` and never retries automatically.
Preserve the canonical state directory during code rollback. Never restore old permission decisions.
This package does not install Apache rules, validate a real owner cookie, send a live test message, or start the home agent.

## Alternate PHP-to-CGI route

Use Python 3.11 or later with SQLite and SSL.
Install the `website` extra in a private virtual environment.
Put `hosting/index.php` only inside the dedicated namespace.
Route every path within that namespace to that bridge.

Publish `entry.js` with `website.js`, `style.css`, and the updated guest page in the same approved release.
If Apache serves public assets directly, include the component's `entry.js` path in its GET/HEAD allowlist.
Keep the existing same-origin script policy. The bootstrap needs no inline-script exception.
Do not update an already staged release in place. Publish both pages, scripts, and the public asset rule together on the next approved release.
PHP 8.2 must support `proc_open` with an array command.

Each request starts one private Python CGI process. No persistent Python listener is required.

The bridge bounds bodies, output, and process duration. It discards provider error text.

Keep both configuration files and the canonical SQLite directory outside the document root.
Keep credentials, paths, real addresses, and generated private cards outside this public repository.
Set `BOOMER_BRIDGE_CONFIG` in server configuration to the private PHP file.
That file returns these fields:

- `python`: absolute virtual-environment interpreter path.
- `release_directory`: selected private code release.
- `python_config`: private JSON configuration path.
- `mount`: dedicated URL prefix without a final slash.
- `owner_identity`: internal owner constant, matching the JSON file.
- `owner_authenticated`: callable for the existing server-side owner-session validator.
- `direct_source`: true only after the direct-source hosting check above.

The validator must check the existing owner session on every private request.
A nonempty cookie or client identity header is not enough.
The PHP bridge sends only valid component cookies to Python.
The existing website's sign-in credential stays inside its current validator.

The private JSON file holds:

- `state_directory`: canonical production state directory.
- `origin`: exact HTTPS origin, without a final slash.
- `mount`: the same namespace prefix.
- `owner_identity`: the same internal owner constant.
- `service_key`: private key of at least 32 characters, shared with the single home agent.
- `idle_poll_seconds`: optional integer from 15 through 60. The default is 15.
- `mail`: the existing website transport settings below.

The production factory is `boomer_switch.website_cgi.create_application`.
It opens the canonical store in production mode and creates the rendezvous owner.
It does not enable the old home callback, exchange, or grant endpoints.
Use the Passenger adapter above for the supported persistent WSGI boundary.
Do not expose the bare production factory without equivalent trusted hosting and owner validation.

## Mail

For SMTP, `mail` holds `transport: smtp`, `host`, numeric `port`, `sender`, and `owner`.
It can also hold `username`, `password`, and Boolean `implicit_tls`.
SMTP always uses certificate-verified TLS. Its socket operations have a 5-second timeout each.
The hosting request limit provides the outer deadline.

For the existing local mail facility, use `transport: sendmail`, an absolute `executable`, `sender`, and `owner`.
Do not provision a new provider.

Registration retains its per-email and global send limits.
Each mail row has one durable claim and stable Message-ID.
Only one validated recipient enters the explicit mail envelope.
Code plaintext leaves durable storage before the external send.

Expired codes and obsolete notices never enter the transport.
Uncertain deliveries remain `unknown` and never retry automatically.
Check the mail provider's authoritative result before a manual recovery.
No production route exposes the demo mailbox.

Production initialization refuses an existing demo or unmarked store.
Demo callers cannot open production state.
A fresh production directory starts with new invitation state.
Later code updates retain that directory, so accepted approvals, invitation URLs, and guest cookies survive.
Do not restore an old database over accepted People decisions.

## Current source and operation contract

`POST /home/poll` requires HTTPS, the private Bearer key, and direct source evidence.
Its body is `{agent_id, sequence, request_id, light}` with canonical UUIDs and a durable increasing sequence.
The first valid poll binds the single agent identity.
Another agent or a lower sequence cannot replace its state.

The `light` field holds only the owner-selected public `light_id`, `name`, and `room`.
Only an approved matching-source browser can receive these labels.
An exact same-source retry does not extend freshness.

The website atomically updates the authenticated source and connection generation.
An address change or a gap of at least 6 seconds starts a new generation.
An authenticated source mismatch on another agent request retires the old epoch until a fresh ordered poll.
An observed guest source change invalidates that guest's open assignments, even after a return to the old source.

After home returns from A through B to A, its old generation remains invalid.
An old or reordered poll cannot restore the old source.
A queued tap expires after 8 seconds and never returns to the queue.
A reconnect cannot revive a tap from before a freshness gap, even on the same address.

The poll response is `{request_id, generation, expires, poll_after, operation}`.
An assignment is `{operation_id, action, session_binding, generation, expires}`.
`action` is `read`, `on`, or `off`. The session binding is a hash, not a browser cookie.
Each operation receives one poll assignment. A later poll never delivers it again.
The home journal prevents duplicate physical dispatch after ambiguous requests or restarts.

Before dispatch, the home agent posts `{agent_id, sequence, request_id, operation_id, generation}` to `/home/authorize`.
The website checks current canonical approval, session expiry, source, freshness, generation, and operation expiry again.
The response is `{allowed, request_id, operation_id, generation, expires}` with at most 2 seconds of authority.
Its deadline never exceeds the source proof or operation deadline.
A browser demand hint alone grants no state or device authority.

The agent posts the same assignment fields plus `outcome` to `/home/result`.
An outcome is `{status, state}` with a bounded confirmed or unavailable result.
An exact result retry is accepted. A conflicting result fails.
A bound result can record truth after a later address change, but it grants no new operation.
Guests cannot read an old generation's result as current state.

Late receipts remain truth, but state freshness uses the operation issue time, not the receipt arrival.
Expired read rows receive a 60-second grace period, then the website removes them.
The runner drops expired read receipts. Mutation deduplication and unresolved effects remain durable.
Completed mutation rows lose obsolete peer and session data. Indexed queue and cleanup queries bound routine scans.

## Cadence and ordinary guest flow

The default idle poll interval is 15 seconds, or 5,760 requests per day.
An operator can select 60 seconds, or 1,440 idle requests per day.
Recent approved browser interest from the matching source selects a 1-second active interval.
A continuously active connection can make 86,400 polls per day.
The home runner enforces at least 1 second between cycles, including failures.

The visible browser checks access every 3 seconds and a pending operation each second.
It pauses while hidden and requests fresh confirmed state at least every 5 seconds while active.
The first visit or reconnect can wait up to the configured idle interval, plus bounded state-query time.
The website waits for a demand-aware fast poll before it accepts work.
The same page shows that wait and automatically checks again. The guest needs no reload.

Offline taps do not enter a durable queue.
Unavailable or uncertain state disables On and Off until a fresh read confirms state.

## Staged installation and rollback

1. Verify runtime support, direct peer semantics, and the IPv4-only origin without a mail or device action.
2. Stage the reviewed release, private configuration, and private production directory.
3. Connect the existing owner validator on the intended origin.
4. Install only the dedicated namespace and its route to the PHP bridge.
5. Exempt public guest routes from inherited whole-site login redirects.
6. Keep People pages and actions behind the current owner validator.
7. Suppress or redact full request targets for invitation and short-link paths before live links.
8. Verify TLS, denied cellular requests, and forged-header rejection before external effects.
9. Run mail and device trials only under their explicit scoped authority.

For rollback, remove the new namespace route or restore the previous reviewed release pointer.
Keep the canonical database, private configuration, and accepted operation receipts.
Stop the single outbound home runner before a release replacement.
Do not delete durable state or start another runner against the same journal.
The old home-entry and callback modules remain historical isolated components, not production fallbacks.

## Private production card

The existing card URLs continue to open this website namespace.
Use the private master and an operator-verified JSON record with `verified: true`, `invite_url`, and `short_url`.
Both URLs use HTTPS and the same namespace: `/guest#invitation=OPAQUE_TOKEN` and `/s/ALIAS`.
The invitation fragment stays in the browser and never enters a new card's HTTP request target.
The generator also accepts a verified legacy `/invite/OPAQUE_TOKEN` record and normalizes its QR to the fragment form.
Legacy printed full links and valid short aliases still redirect to the same guest page.
They do not create sessions, send codes, or replace cookies during a preview GET.
The page first restores its Strict cookie through a same-origin access check, then accepts the invitation through a protected POST.
It retains a pending invitation until the server acknowledges it and never retries mail actions automatically.
The clean guest bookmark contains no invitation. Responses use `Referrer-Policy: no-referrer`.
Keep request bodies out of logs and redact legacy invitation paths at the host until old cards are retired.
Application handlers do not log invitation values. Existing staged releases stay unchanged until the next separately approved deployment.
The generator does not create live links or make network requests:

```text
python -m boomer_switch.website_card --verified-urls PRIVATE_RECORD --template PRIVATE_MASTER --output PRIVATE_CARD
```

Install the `pdf` extra for this local command.
The renderer protects both private inputs, preserves the master, and removes demo-only labels in production mode.
Keep the master, record, and generated card private.
