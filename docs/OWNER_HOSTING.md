# Owner website component

`boomer_switch.people.create_people_application` creates the reusable People application for an authenticated WSGI website.
The website owns People permissions. Device credentials and control remain outside this component.
Production hosting remains unverified and undeployed.
The current website-first transport is defined in [Hosted registration and controls](HOSTED_REGISTRATION.md).
The standalone WSGI factory below retains its original authentication contract for isolated consumers.
Install the `website` extra for this component's runtime dependency without the browser demo tools.

## Required hosting contract

1. Require the website's existing sign-in before this application.
2. Set `REMOTE_USER` from the server-verified identity after authentication.
3. Pass the exact configured owner identity to `create_people_application`.
4. Serve the component through HTTPS with a trusted `wsgi.url_scheme` value of `https`.
5. Mount the complete application under one dedicated WSGI prefix with the correct `SCRIPT_NAME` and `PATH_INFO` values.
6. Route the owner session to the same application process.

Never copy a client header into `REMOTE_USER`.
The adapter ignores `HTTP_REMOTE_USER` and other client identity headers.
Missing, different, or unverified owner identity denies every page, asset, read, and action.
The adapter checks the current server identity and session again after each mutation request body arrives.

The adapter keeps owner sessions and their CSRF tokens in process memory for 1 hour.
Use one process or sticky routing to that session's process.
A restart requires a fresh page load and token. Persisted People decisions survive that restart.

Each mutation requires the session-bound `X-CSRF-Token`.
An owner cookie alone does not authorize it.
HTTPS owner cookies use `Secure`, `HttpOnly`, and `SameSite=Strict`.
The cookie path uses the component's WSGI mount prefix, with `/` only for a root mount.

Mount `/people`, `/people.js`, `/style.css`, and `/api/people` inside the same prefix.
The page uses relative asset and API URLs, so it does not claim the host website's root paths.
For example, a mount at `/house-lights` exposes its page at `/house-lights/people`.
The host must keep this component separate from guest routes.

```python
from pathlib import Path
from boomer_switch.people import create_people_application

application = create_people_application(
    Path("data/website-permissions"),
    owner_identity="CONFIGURED_OWNER_IDENTITY",
)
```

Keep the configured identity and state directory in private server configuration.
The example does not create sign-in, configure a reverse proxy, or establish the real permission transport.
Unknown website permission or current home internet source must still deny any future real-device command.

## People decisions

Only verified pending requests appear in Pending requests.
Preapproval creates permission for an email, but a guest browser still requires inbox proof.
Approval has no end date unless the owner sets one.
Each accepted change increments the person's revision. A stale or replayed action returns a conflict without an overwrite.
Denial and removal invalidate that person's existing guest sessions and verification codes.

The owner selects an inclusive last day in the browser's displayed time zone.
The browser converts midnight after that day to the stored Unix deadline.
The page also shows the exact local end time. A deadline check uses that instant on each guest command.
The owner can change or clear a future deadline without loss of active access.
After expiration, restoration requires fresh inbox proof and invalidates sessions and codes from the prior grant.

The local demonstration shares SQLite only to verify the permission consumer through ordinary guest commands.
The standalone demo does not establish the current production source contract or host configuration.

## Hosted registration and PHP integration

[Hosted registration install](HOSTED_REGISTRATION.md) describes the production guest routes, existing mail transport, and private People adapter.
It defines same-origin controls, current home internet source checks, outbound agent requests, and scoped PHP-to-CGI installation.
This adapter persists owner session tokens for per-request Python processes.
It still requires the current website owner identity on every private request.
The existing standalone WSGI factory above keeps its original in-process session contract.
Host validation and an authorized acceptance demo remain required before deployment.
