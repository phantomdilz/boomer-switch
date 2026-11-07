# Boomer Switch — House Lights

A mobile-friendly web page that lets invited, owner-approved guests turn household smart lights on and off from a phone browser. No app install, no shared owner credentials, no reaching for a lamp's physical switch.

**Status:** local sample-light demo. Guest access remains planned.

## Goals
- Guests on the home's main Wi-Fi or guest Wi-Fi control selected lights from a browser and a bookmark.
- Lights grouped by room with recognizable names (e.g. "Lamp beside the sofa"), large On/Off controls.
- State shown is what the device reports; "Unavailable" when it cannot be confirmed.
- Invitation + verified email + owner approval; access revocable, optional end date.

## Architecture
- **Guest web app** — House Lights page and registration flow.
- **Home service** — always-on home server running [python-kasa](https://github.com/python-kasa/python-kasa) on the main network; holds device credentials and a server-enforced device allowlist. Only the guest service is published via [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel) (no port forwarding, no fixed public IP).
- **Owner admin** — private People page on the owner's website, behind the owner's sign-in. The website manages permissions and sends verification codes and owner notices through its existing mail system.
- **Command checks** — the home service must verify current website permission before each command. It must also confirm that the guest uses the home's main Wi-Fi or guest Wi-Fi. Each command must fail closed if either check fails or cannot confirm access. Remote control is not allowed.

See [docs/BRIEF.md](docs/BRIEF.md) for the full brief and [AGENTS.md](AGENTS.md) for contribution rules.

## Local control boundary

`boomer_switch.lights.LightService` enforces the selected devices and individual outlets before each command.
It accepts only a configured `light_id` and the actions `on` or `off`.
It never commands a strip parent or an unselected outlet.
The display list contains only each light's ID, name, and room.

Copy `devices.example.json` to the ignored file `config.local.devices.json`.
Replace its placeholders with owner-selected values from local discovery.
Use `{"lights": []}` to select no lights.
Keep all real inventory, addresses, identifiers, and credentials in ignored local files.
An absent or invalid allowlist stops configuration. It never selects discovered devices by default.

Load the file with `load_allowlist(Path("config.local.devices.json"))`.
Pass those entries and a trusted async resolver to `LightService`.
The resolver accepts the configured parent `device_id` and returns the matching python-kasa device.
An `outlet_id` selects one exact child device ID under that parent.

Use `null` only for a standalone bulb, plug, or lighting dimmer.
Other device types are unavailable in this first boundary.
Reload the service after the owner changes the selection.

The service refreshes device state before control and after each command.
It returns `on`, `off`, or `unavailable` from confirmed device state.
A timeout or uncertain command returns `unavailable`. The service does not retry commands.
The default total timeout is 5 seconds, including the wait for another command on the same device.
Provider errors and private device IDs never appear in the result.

This module has no HTTP endpoint or guest access check.
The future server caller must check current guest permission before each call.
It must keep the resolver and local configuration private.
Local synthetic tests do not establish compatibility with installed hardware.

## Local mobile demo

Run `python -m boomer_switch.demo --port 0` from the local virtual environment.
Open the loopback URL that the command prints.
The page groups sample lights by room and shows their confirmed state.
Choose On or Off to control one sample light.

The page checks state every 3 seconds while visible. Each request has a 3-second timeout.
Failed requests show Unavailable. The page never assumes that a command succeeded.

This demo uses only fixed sample devices in memory.
It binds only to loopback and has no real-device or private-configuration option.
It does not implement guest access. A real guest service still requires the planned permission checks.
Stop the demo with Ctrl+C.

The optional `demo` extra declares Playwright for `demos/mobile_acceptance.py`.
That script checks the mobile page with Firefox and uses only sample devices.

For synthetic external changes, send JSON to `POST /demo/fixtures`:
`{"light_id":"sofa-lamp","state":"on"}`.
Use `off` for an external off state or `unavailable` for a persistent sample failure.
These fixtures affect only the sample devices.

## Synthetic tests

Use Python 3.11 or later.
Install the development dependencies in a local virtual environment with `python -m pip install -e ".[dev]"`.
Run `python run_tests.py full --workers 2` from that environment.
The `smoke`, `lights`, `demo`, and `tooling` tiers also accept ordinary pytest selectors.
Use `--durations 10 --slow-threshold 1` for a duration report.

The runner uses separate temporary paths and parallel workers. The tests never contact real devices.
The runner rejects overrides for its temporary paths, cache directory, and pytest `addopts`.

## License

This project uses the [MIT License](LICENSE).

## Validation still required

The exact method to confirm guest presence on the home's main Wi-Fi or guest Wi-Fi still requires implementation and validation.
Authenticated HTTPS remains a proposed transport for the website permission check.
The transport and authentication method still require validation before implementation.
