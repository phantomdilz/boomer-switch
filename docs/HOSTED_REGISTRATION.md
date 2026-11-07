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

## PHP and private Python

Use Python 3.11 or later with SQLite and SSL.
Install the `website` extra in a private virtual environment.
Put `hosting/index.php` only inside the dedicated namespace.
Route every path within that namespace to that bridge.
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
A direct WSGI host must supply a trusted HTTPS scheme, `REMOTE_ADDR`, and server-only `boomer.direct_peer=True`.
It must also enforce the equivalent 25-second total request limit.
Never derive these values from client headers.

## Mail

For SMTP, `mail` holds `transport: smtp`, `host`, numeric `port`, `sender`, and `owner`.
It can also hold `username`, `password`, and Boolean `implicit_tls`.
SMTP always uses certificate-verified TLS. Its socket operations have a 5-second timeout each.
The PHP process limit provides the outer deadline.

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
Both URLs use HTTPS and the same namespace, with `/invite/OPAQUE_TOKEN` and `/s/ALIAS` paths.
The generator does not create live links or make network requests:

```text
python -m boomer_switch.website_card --verified-urls PRIVATE_RECORD --template PRIVATE_MASTER --output PRIVATE_CARD
```

Install the `pdf` extra for this local command.
The renderer protects both private inputs, preserves the master, and removes demo-only labels in production mode.
Keep the master, record, and generated card private.
