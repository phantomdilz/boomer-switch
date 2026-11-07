# Project brief — House Lights

## Summary
A mobile-friendly web page lets invited guests turn household lights on and off from their phone browser.
Guests must use the home's main Wi-Fi or guest Wi-Fi.
They can use a bookmark on return visits without a vendor app, VPN client, shared credentials, or a physical switch.

The guest page groups lights by room with names a visitor recognizes. Each light has large On and Off controls. The page shows the state reported by the device and displays **Unavailable** when it cannot confirm that state.

## Guest registration and access
1. The guest scans a QR code on a printed card or types the short URL printed beneath it. The QR holds the full invitation URL; the short link comes from a self-hosted URL shortener (e.g., YOURLS) and opens the same invitation.
2. The guest enters a name and verifies their email with a one-time code. Only a valid invitation can start registration. The website sends the code through its existing mail system. It also sends one owner notice per pending, verified guest through that mail system. Repeat submissions do not send duplicate notices.
3. The owner approves or denies. After approval the waiting page opens the controls automatically. The bookmarkable URL contains no invitation secret.
4. Access lasts until removed by default; the owner can set an optional end date. An approved guest on a new phone can re-verify the same email without another approval.

The QR is generated locally. The short-link keyword is random and easy to type. If the card leaks, the owner retires the invitation and its short link; existing approvals and bookmarks keep working.

## Owner administration
A private People page on the owner's website, behind the owner's sign-in:
- **Pending requests** — review name and verified email; approve or deny with one tap.
- **Add person** — preapprove an email; the guest still verifies it at first sign-in.
- **Approved people** — remove access, or set/change/clear an end date.

The home service enforces removal and expiration on the next light command, including from an already-open page. Device credentials and control stay on the home server.
Before each command, the home service must verify current website permission.
It must also confirm that the guest uses the home's main Wi-Fi or guest Wi-Fi.
Each command must fail closed if either check fails or cannot confirm access.

## Network and device design
- Only the guest service is published from an always-on home server via Tailscale Funnel (HTTPS, normal browser). The home router's guest-network isolation stays enabled across all mesh nodes. No port forwarding or fixed public IP required.
- Guest controls require the home's main Wi-Fi or guest Wi-Fi. Remote control is not allowed, even with current owner approval.
- python-kasa on the main network: read-only discovery first, then the owner selects which bulbs and individual outlets appear; the allowlist is enforced server-side. Pilot devices: HS103 plug, LB130 bulb, KL130 bulb (listed as supported by python-kasa; installed hardware/firmware still need a local check).

## Milestones
1. Discover and label pilot devices
2. Build the mobile light controls
3. Add invitations and verified registration
4. Build the owner's People page
5. Verify the full guest experience

## License

This project uses the [MIT License](../LICENSE).

## Validation still required

The exact presence check still requires implementation and validation.
The presence check must confirm guest access through the main Wi-Fi or guest Wi-Fi while guest-network isolation stays enabled.
Authenticated HTTPS remains a proposed transport for the website permission check.
The transport and authentication method still require validation before implementation.
