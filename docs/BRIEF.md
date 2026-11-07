# Project brief — House Lights

## Summary
A mobile-friendly web page lets invited guests turn household lights on and off from their phone browser. Guests stay on the home guest Wi-Fi and can use a bookmark on return visits without installing vendor apps or a VPN client, borrowing the owner's credentials, or touching a lamp's physical switch.

The guest page groups lights by room with names a visitor recognizes. Each light has large On and Off controls. The page shows the state reported by the device and displays **Unavailable** when it cannot confirm that state.

## Guest registration and access
1. The guest scans a QR code on a printed card or types the short URL printed beneath it. The QR holds the full invitation URL; the short link comes from a self-hosted URL shortener (e.g., YOURLS) and opens the same invitation.
2. The guest enters a name and verifies their email with a one-time code. Only a valid invitation can start registration. The owner gets one notification per pending, verified guest; repeat submissions do not send duplicate notices.
3. The owner approves or denies. After approval the waiting page opens the controls automatically. The bookmarkable URL contains no invitation secret.
4. Access lasts until removed by default; the owner can set an optional end date. An approved guest on a new phone can re-verify the same email without another approval.

The QR is generated locally. The short-link keyword is random and easy to type. If the card leaks, the owner retires the invitation and its short link; existing approvals and bookmarks keep working.

## Owner administration
A private People page on the owner's website, behind the owner's sign-in:
- **Pending requests** — review name and verified email; approve or deny with one tap.
- **Add person** — preapprove an email; the guest still verifies it at first sign-in.
- **Approved people** — remove access, or set/change/clear an end date.

The home service enforces removal and expiration on the next light command, including from an already-open page. Device credentials and control stay on the home server.

## Network and device design
- Only the guest service is published from an always-on home server via Tailscale Funnel (HTTPS, normal browser). The home router's guest-network isolation stays enabled across all mesh nodes. No port forwarding or fixed public IP required.
- Expected use is at home. The initial design relies on verified identity and owner approval, so an approved guest could operate lights remotely while access is active. A strict home-only rule is an open decision.
- python-kasa on the main network: read-only discovery first, then the owner selects which bulbs and individual outlets appear; the allowlist is enforced server-side. Pilot devices: HS103 plug, LB130 bulb, KL130 bulb (listed as supported by python-kasa; installed hardware/firmware still need a local check).

## Milestones
1. Discover and label pilot devices
2. Build the mobile light controls
3. Add invitations and verified registration
4. Build the owner's People page
5. Verify the full guest experience

## Open decisions
Email delivery method; owner notification channel; how the home service checks permissions with the website (decline if it cannot verify); strict home-only rule; license.
