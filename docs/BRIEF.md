# Project brief — House Lights

## Summary
A mobile-friendly web page lets invited guests turn household lights on and off from their phone browser.
Guests must use the home's current internet egress and a verified, approved website session.
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
The website must also match the guest's direct source to a fresh authenticated home poll before each read or command.
Each command must fail closed if either check fails or cannot confirm access.

## Network and device design

- The external HTTPS website is the guest's only origin for invitations, registration, light controls, and bookmarks.
- A single home agent makes authenticated outbound polls. There is no inbound home service, callback, guest app, or tunnel requirement. Guest-network isolation stays enabled.
- The website learns the current home internet address from the direct server-observed source of an authenticated poll.
- The website automatically replaces that source after an ISP address change. Existing approvals and bookmarks remain valid.
- A source change or freshness gap invalidates old operation generations. Offline taps never replay after reconnect.
- This IPv4 pilot requires a validated IPv4-only application origin. It rejects IPv6 sources and never infers a shared IPv6 prefix.
- Shared internet egress includes home Wi-Fi, wired clients, and a VPN through home. The operator accepted this boundary.
- A cellular or other remote connection fails closed, even with current owner approval.
- Unknown effects block new mutations until an authorized confirmed read. Each operation has one lease and fresh authorization before physical dispatch.
- Device credentials and python-kasa stay on the home server. The owner-selected allowlist remains authoritative.
- Pilot devices: HS103 plug, LB130 bulb, and KL130 bulb. Installed hardware and firmware still require a local check.

## Milestones
1. Discover and label pilot devices
2. Build the mobile light controls
3. Add invitations and verified registration
4. Build the owner's People page
5. Verify the full guest experience

## License

This project uses the [MIT License](../LICENSE).

## Validation still required

The actual hosting runtime, direct-source topology, and IPv4-only route need an authorized live check.
Real mail and physical device acceptance remain separate from synthetic test evidence.
[Hosted registration and controls](HOSTED_REGISTRATION.md) defines the approved source and outbound-agent contract.
