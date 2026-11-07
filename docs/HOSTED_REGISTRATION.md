# Hosted registration install

This component uses the existing website mail service and owner sign-in.
It does not control devices or prove home Wi-Fi access.
The home service must enforce its network boundary before each command.
The host runtime and the acceptance demo still require operator checks.

## Runtime choices

Use Python 3.11 or later with SQLite and SSL support.
Install the `website` extra in a private virtual environment.
A WSGI host can call `boomer_switch.website_cgi.create_application` with private configuration.
Compose its `grant_app` from `boomer_switch.website_grants.create_website_grant_application` with the same database and service key.
This is the PJ-60 component. Do not copy its grant rules.

On a PHP-only host, use `hosting/index.php` inside a new dedicated namespace.
It calls the Python module once per request through `proc_open`.
It does not need a persistent Python listener.

The bridge bounds request bodies, response bodies, and process duration.
PHP must support `proc_open`, an array command, and the configured Python interpreter.
The bridge expects Linux `/dev/null` and PHP 8.2.
Check these facts before installation.

## Private configuration

Keep both configuration files and the SQLite directory outside the document root.
Set restrictive file permissions for the website account.
Do not read or copy the existing authentication secrets into the release.
Do not store real values in this public repository.

The private PHP file returns an array with these keys:

- `python`: absolute path to the private virtual environment's Python executable.
- `release_directory`: absolute path to the selected immutable application release.
- `python_config`: absolute path to the private JSON configuration file.
- `mount`: the dedicated URL prefix, without a final slash.
- `owner_identity`: an internal constant that matches the Python configuration.
- `owner_authenticated`: a callable that invokes the existing site's owner-session validator on every request.

Set the server environment variable `BOOMER_BRIDGE_CONFIG` to this private PHP file.
The callable must validate the current owner cookie through the existing server-only configuration.
A cookie name, header, or nonempty cookie value alone is not enough.
If the existing site has no callable validator, adapt its existing validator in private code.
Keep that adapter outside the public document root and repository.
Never allow a client to choose `REMOTE_USER`, the private configuration path, or the interpreter.

The private JSON file has these keys:

- `state_directory`: private canonical state directory. Its database is `registration.sqlite`.
- `origin`: exact HTTPS website origin, without a trailing slash.
- `mount`: the same dedicated URL prefix.
- `owner_identity`: the same internal owner constant.
- `service_key`: at least 32 private characters, shared only with the home service.
- `home_callback`: exact HTTPS callback URL on the home guest service, without a query or fragment.
- `mail`: the transport settings below.

For SMTP, `mail` holds `transport: smtp`, `host`, numeric `port`, `sender`, and `owner`.
It can also hold `username`, `password`, and Boolean `implicit_tls`.
SMTP always requires certificate-verified TLS. Plain SMTP is unavailable.

For the existing local mail facility, use `transport: sendmail`, an absolute `executable`, `sender`, and `owner`.
Use the existing website transport. Do not provision another mail provider.

## Scoped staged install

1. Confirm the runtime checks above without a mail send or a private-secret read.
2. Create a new private release directory and install the reviewed source and `website` extra there.
3. Create a new private state directory and private configuration files.
4. Wire the private owner validator to the existing site's current sign-in.
5. Create only the dedicated namespace in the public document root.
6. Put the PHP bridge there and route every path within that namespace to it.
7. Exempt public guest paths from any inherited whole-site login redirect.
8. Keep `/people`, `/people.js`, and `/api/people` behind the current owner validator.
9. Keep the rest of the website's root, login, files, and routes unchanged.
10. Disable URL query logging for the home callback and invitation paths.
11. Verify HTTPS and private-route rejection before a scoped live mail demo.
12. Test one guest code and one owner notice only with explicit mail authority.

No live mail, public upload, or production configuration read forms part of local verification.
If a route rewrite, authentication hook, or transport setting is unknown, retain the staged release privately.
Do not route public requests to an incomplete bridge.

## Guest and home session contract

The website cookie is Secure, HttpOnly, and SameSite=Strict.
Its path is the component namespace. It never becomes a home session.
The home front door creates a browser-bound random challenge and state.
It opens the website guest page with `#challenge=OPAQUE_CHALLENGE&state=OPAQUE_STATE`.
The website removes the fragment and keeps these values in that tab's session storage.

After approval, the guest selects **Open home controls**.
The same-origin request requires the website's CSRF token and exact Origin.
The website returns the fixed configured home callback with a short code and state.
The browser cannot choose a callback URL.

The home callback must match state to its pending browser cookie before any exchange.
Its server posts `{code, challenge, request_id}` to `/exchange` with the private Bearer service key over HTTPS.
The response holds `{session, expires, request_id}`.
The home server checks the request ID and expiry before it issues its own HttpOnly cookie.

Codes expire after 60 seconds. Different replays fail.
An exact retry returns the same token after an ambiguous network result.
The home token maps to the canonical verified website session through `home_sessions`.

The PJ-60 `/grant` adapter reads that map and current People permission for each command.
The browser never receives the service key or a website cookie in a URL.
No cross-domain cookie sharing is required.

## Mail outcomes and recovery

Registration keeps its existing per-email and global send limits.
Each durable mail row has one claim and one stable Message-ID.
The worker clears code plaintext before an external send.
It sends at most 4 messages per request, with a 5-second timeout for each transport operation.
Owner notices use the canonical verified pending revision, so repeat verification does not duplicate a notice.
No production route exposes the fake mailbox or sample controls.

A failed or interrupted send stays `unknown` in `mail_delivery`.
The application does not retry that row automatically because the provider can already have accepted it.
Check the existing mail provider's authoritative result before any manual recovery.
A guest can request a new code after the existing delay and quota checks.
Provider errors and message contents never enter the public response.

## Rollback

Remove only the new namespace route or restore its previous staged pointer.
Stop any task-owned CGI processes before a release replacement.
Keep the private database and configuration in place for recovery.
Keep state, the website root, and the existing login unchanged.
Do not restore an old database over accepted People decisions.

Reinstall the prior reviewed code only when its schema remains compatible.
The added tables do not change the existing registration tables.
