# Project brief — House Lights

## Summary
A mobile-friendly web page lets invited guests turn household lights on and off from their phone browser.
Guests use the public website while their request shares the home's observed public internet egress, normally through the main Wi-Fi or guest Wi-Fi.
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
The website must also match the guest's current server-observed public IPv4 source to a fresh authenticated home connection.
Each command must fail closed if either check fails or cannot confirm access.

## Network and device design
- The public hosted website serves guest registration and controls. An always-on home runner polls it over authenticated outbound HTTPS. No home listener, guest-side computer, inbound port, or fixed public IP is required. Guest-network isolation stays enabled.
- The website learns the home source from authenticated connections and refreshes it automatically after ISP changes. Guest controls require current permission and matching fresh public egress. This approved check does not prove physical Wi-Fi presence and cannot distinguish a shared NAT or on-site relay.
- Operations have short expiry, one lease, and fresh authorization immediately before physical dispatch. Unknown outcomes block new mutations until an authorized confirmed read. The browser keeps its ordinary website bookmark and verified session across ISP changes.
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

The real hosted adapter must expose trustworthy client sources rather than a shared frontend proxy address.
Public IPv4 matching, automatic ISP rotation, live registration mail, and the selected lamp still require private end-to-end validation.
IPv6, unverified source topology, stale home connections, and unknown permission fail closed in the first outbound pilot.
