# Boomer Switch — House Lights

A mobile-friendly web page that lets invited, owner-approved guests turn household smart lights on and off from a phone browser. No app install, no shared owner credentials, no reaching for a lamp's physical switch.

**Status:** planning

## Goals
- Guests on the home guest Wi-Fi control selected lights from a normal browser and a bookmark.
- Lights grouped by room with recognizable names (e.g. "Lamp beside the sofa"), large On/Off controls.
- State shown is what the device reports; "Unavailable" when it cannot be confirmed.
- Invitation + verified email + owner approval; access revocable, optional end date.

## Architecture
- **Guest web app** — House Lights page and registration flow.
- **Home service** — always-on home server running [python-kasa](https://github.com/python-kasa/python-kasa) on the main network; holds device credentials and a server-enforced device allowlist. Only the guest service is published via [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel) (no port forwarding, no fixed public IP).
- **Owner admin** — private People page on the owner's website, behind the owner's sign-in. The website manages permissions; the home service checks them on every command and fails closed.

See [docs/BRIEF.md](docs/BRIEF.md) for the full brief and [AGENTS.md](AGENTS.md) for contribution rules.

## Open decisions
- Email delivery method for one-time codes
- Owner notification channel
- Website ↔ home-service permission check (must fail closed)
- Strict home-only rule vs. remote use while access is active
- License (none chosen yet)
