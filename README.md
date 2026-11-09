# House Lights

A phone page for invited guests to turn selected home lights on and off. Guests use the browser they already have. They do not get the owner's login or the device passwords.

## How it works

The website sends an invitation, checks the guest's email, and waits for the owner to approve. A small program on the home network polls that website over outbound HTTPS and switches only the lights on a private allowlist. It does not open a port on the home router.

The guest has to share the house's current public IPv4 address. A phone on cellular data is refused. Matching addresses show a shared internet connection. They do not prove which Wi-Fi the phone joined.

## Try the sample

Requires Python 3.11 or newer.

```bash
python -m pip install -e ".[dev]"
python -m boomer_switch.demo --port 0
```

Open the `http://localhost:` address the command prints. The lights are fake, and the server listens only on this computer.

Registration uses the same sample lights and a fake mailbox:

```bash
python -m boomer_switch.registration_demo serve --state-dir data/registration-demo --port 0
```

`python -m boomer_switch.registration_demo operator --help` lists the local card, mailbox, approve, and revoke commands.

The owner page, still on sample data:

```bash
python -m boomer_switch.people_demo serve --state-dir data/people-demo --guest-port 0 --owner-port 0
```

## Run it at home

Copy the examples and fill them in on the home machine. Do not commit the copies.

- `devices.example.json` to `config.local.devices.json`
- `home.example.json` to `config.local.home.json` for one light, or `home-fleet.example.json` for several

```bash
python -m pip install -e ".[pilot]"
python -m boomer_switch.home_runner --config config.local.home.json
```

Website install, mail, and owner alerts: [docs/HOSTED_REGISTRATION.md](docs/HOSTED_REGISTRATION.md), [docs/OWNER_HOSTING.md](docs/OWNER_HOSTING.md), [docs/OWNER_NOTIFICATIONS.md](docs/OWNER_NOTIFICATIONS.md). Design notes: [docs/BRIEF.md](docs/BRIEF.md). Home runner: [docs/PILOT_BACKEND.md](docs/PILOT_BACKEND.md).

PDF invitations need the `pdf` extra and a private template that stays outside this repository.

## Tests

```bash
python run_tests.py full --workers 2
```

The suite uses sample devices and does not call a real network. Browser checks in `demos/` need Firefox and `pip install -e ".[demo]"`. `run_tests.py` does not run them.

## License

[MIT](LICENSE).
