# Outbound selected-light home connection

Guests use the public website for registration, bookmarks, and light controls.
The home runner makes authenticated outbound HTTPS requests to that website.
It opens no home or guest listener and needs no guest-side computer, inbound port, fixed public IP, or overlay network.
The website manages verified identity and owner permission. Device credentials and control stay at home.

## Private configuration and entry point

Install the `pilot` extra in an isolated Python 3.11-or-later environment.
Copy `home.example.json` to the ignored `config.local.home.json` and replace its placeholders privately.
For single-lamp mode, use the existing private allowlist and select one standalone lamp by `light_id`.
Discovery uses `device_host` only as a hint. The resolver checks the exact configured device ID and type before every operation.
Optional device `username` and `password` fields must both be present when needed.

```text
python -m boomer_switch.home_runner --config config.local.home.json
```

`python -m boomer_switch.pilot` is a compatibility alias for the same outbound entry point.
Relative file paths resolve from the configuration file's directory.
The selected physical identity and website URL bind the operation journal. A different lamp or website cannot reuse it.
On upgrade, keep the existing physical target's journal path in `operation_db` so unresolved dispatch records remain effective.

The journal creates a stable random agent ID and saves the next poll sequence before each network try.
Run one home process per journal. The process lock rejects a second normal runner.

### Multiple selected lights

Use `home-fleet.example.json` for 1 through 32 standalone lights.
List each selected public ID in `light_ids` and supply its private discovery hint in `device_hosts`.
Every selected ID must exist in the private allowlist. Extra discovery hints and excluded IDs stop configuration.
The `light_id` field retains the original journal lamp and must remain selected.
The remaining fields and command-line entry point stay the same. Omit the single-lamp `device_host` field in this mode.
Add `--check-config` to either Python entry point to validate the selection without device queries, network calls, or journal changes.
The home runner never discovers or controls an unselected device.

Stop the existing runner before the upgrade. Retain its journal and operation receipts.
The normal entry point obtains the journal lock before configuration can migrate the journal.
The migration keeps the physical binding, agent ID, poll sequence, receipts, and command marks.
It binds legacy command marks only to the original physical lamp.
Each new target has a saved physical binding. Reusing its public ID for a different physical device fails closed.
The new code rejects single-lamp mode on a migrated journal. Pre-upgrade binaries cannot detect this new journal marker.
Never let a pre-upgrade binary open a fleet journal: its global read reconciliation can clear another lamp's uncertain marks.
A rollback must retain a fleet-compatible journal reader and all accepted results.
Keep the stopped journal's backup for recovery and reconcile unresolved operations before any rollback that changes its format.

A confirmed read reconciles uncertain commands only for that physical lamp.
An uncertain lamp A does not block lamp B after the accepted receipt reaches the website.
One operation runs at a time. A transport failure retains the accepted result for receipt recovery before another assignment.
Approved, on-home, visible pages request automatic state updates through the existing `POST /api/lights` route.
The server admits at most one automatic read every two seconds across all guests, and at most one read per target every ten seconds.
It coalesces concurrent callers instead of adding a scan per phone. Every third admission serves the oldest target across the complete catalog; other admissions can prefer visible lights.
Each read still needs the current verified session, permission, fresh home source, target binding, and normal machine authorization.
These reports never authorize a command. The home runner and its final dispatch guard are unchanged.
A foreground tap or explicit refresh cancels only an unleased automatic read. It can wait behind one leased read, not a complete scan.
The original eight-second operation expiry still applies. A stalled read can make a waiting tap expire; the application never extends or replays that tap.

Each visible guest sends at most 30 automatic requests per minute while healthy; those requests replace ordinary access polling.
Failures back off up to 30 seconds. Hidden pages and lost access stop new automatic requests; an already accepted read can finish.
The device budget is 30 automatic **read operations** per minute globally. The unchanged runner can make two device update calls per read operation.
State reports come from the canonical operation receipts. Indexed queries retain the newest command truth as well as read truth.
At most two old read rows per current light remain for its latest attempt and confirmation. Old session/source fields are cleared, and superseded read rows are pruned.
An uncertain local dispatch cannot be cleared by an older cached report, including when the enqueue acknowledgement was lost.
It needs a newer confirmed observation or an explicit confirmed refresh. Source and permission failures still clear private labels and controls.

The UI keeps the last confirmed On or Off with its age and marks reports older than 45 seconds stale.
An unavailable or unknown result remains distinct from a stale report and never becomes a confirmed Off.
The bounded synthetic probe is `python demos/automatic_updates_probe.py`; it makes no network or device calls.
Its two-guest, 15-light virtual sweep observed all targets in 62 seconds, with 44 read operations and 88 device update calls over 90 simulated seconds.
Under simulated 50 ms HTTP/device calls, wall-clock tap-to-actuation was about 0.64 seconds with no read or an unleased read, and 1.16 seconds behind a read held for 250 ms.
These are fixture measurements, not household latency guarantees. A slow or unreachable device can increase a full pass; visible room hints do not starve the remaining catalog.
`demos/multi_light_acceptance.py` now checks automatic changes, stale age labels, hidden stop, room focus, priority controls, and the older explicit-read fallback.
This replaces its former no-background-read criterion; domain tests retain the global cap, two-guest fairness, target isolation, expiry, and authorization distinctions.

A guest's explicit refresh or command still supplies the exact target ID.
The website permits a direct On or Off request for a selected light without a separate cached read.
The home runner refreshes that exact device before its final dispatch authorization and confirms state after the command.
Its journal still blocks uncertain commands until a confirmed read of that physical light.

The guest catalog contains only public IDs, names, and rooms. It appears only after approval and current source checks.
Reachability comes from the requested device read. A failed or uncertain read is unavailable, not confirmed Off.
The UI receives `online` for a fresh confirmed result, `unknown` for an uncertain or expired result, and `unavailable` otherwise.
These values do not prove whether an unresponsive lamp has power.

## Approved network rule

The website learns the home's current public IPv4 source from an authenticated home poll.
It compares that source with the server-observed source of the approved guest's current website request.
The guest cannot supply an address, forwarded header, device host, or location flag as authority.
The approved network check uses matching public internet egress. It does not prove a phone's physical Wi-Fi attachment.

Shared public NAT or an on-site relay can have the same observable source. The operator must accept that limit.

The public host must expose trustworthy direct client sources to the website adapter.
A frontend proxy address shared by unrelated clients is not acceptable evidence.
IPv6, unverified forwarding topology, stale source information, and unknown permission fail closed in this pilot.
Validate the actual hosting topology before any live command.

An ISP address change needs no manual IP update.
The next authenticated poll adopts the new source and advances the server generation.
Old assignments cannot regain authority, including after an A-to-B-to-A address change.
If the home connection goes stale, its old generation cannot authorize queued work.

## Fixed machine API

The configured website URL is a fixed HTTPS prefix. The runner posts only to these routes:

- `/home/poll` receives `{agent_id,sequence,request_id,light}`.
- `/home/authorize` receives `{agent_id,sequence,operation_id,generation,request_id}`.
- `/home/result` receives those assignment fields and an `outcome` object.

Multi-light polls replace `light` with `lights` and a stable `catalog_revision` for the selected physical bindings and metadata.
Their assignments, authorization requests, and result receipts include the exact `light_id`.
The catalog has at most 32 entries. Only `/home/poll` permits a body of up to 131072 bytes, including escaped Unicode labels.
Other request limits stay unchanged. A catalog revision change retires prior dispatch authority but retains accepted operation truth.
Legacy single-lamp poll, assignment, and receipt shapes remain supported.

Each request uses the private service bearer key and verified TLS.
Requests have a 2-second total timeout, bounded response size, no redirects, no environment proxy, and no automatic retry.
No public IP is stored in home configuration or sent as a claimed source.
The website binds one agent to the service key. Agent recovery requires an explicit operator action.

The authenticated `light` object contains only the selected public `light_id`, name, and room.
The website exposes those labels only after current approval and egress checks. It never receives the physical device ID or device host.

Poll responses bind the request ID, positive generation, source freshness deadline, optional operation, and `poll_after`.
An operation contains only its UUID, `read`/`on`/`off` action, session hash, generation, and expiry.
The home runner routes only to its privately selected lamps. The website cannot supply a network address or physical device identity.
The source proof lasts at most 6 seconds. Operations expire within 8 seconds and are leased once, with no offline replay.

The website normally requests a 15-second idle poll interval and a 1-second active interval.
Its private idle setting supports 15 through 60 seconds. The home runner enforces a 1-through-60-second response range and never spins.
Default idle polling permits about 5,760 polls per day. A 60-second idle interval permits about 1,440.

Continuous active demand permits at most 86,400 polls per day, plus bounded authorization and result calls for leased work.
The first control request after idle can wait for the next poll. The website must show that wait instead of queueing offline work.

## Dispatch and recovery

The runner checks current permission and source generation before discovery.
After device refresh, it checks both again at the actual command boundary.
The final grant lasts at most 2 seconds and cannot outlive the operation.
It checks time again after the durable dispatch record and before the possible physical effect.
The website and home clocks must agree closely enough for these short deadlines.

The device protocol uses zero SDK retries. The command journal reserves the operation UUID before any possible mutation.
A failed or uncertain device result is **Unavailable**, with an unknown outcome when dispatch began.
An unknown or crash-left dispatch blocks new mutations with other UUIDs across restart.
Only a newly authorized, confirmed device read reconciles that uncertainty.

The runner saves the outcome and a stable result request ID beside the command record before it reports success.
If an acknowledgement is lost, it resends the same saved result before it polls for another assignment.
It does not repeat the command. A pending unacknowledged outcome blocks further physical dispatch.
The website accepts an original operation's result after an ISP change only to record truth, with no new control authority.
Results that conflict stop the runner and require an operator check with the retained journal.

Acknowledged reads leave no receipt. An expired unacknowledged read can be discarded because it has no physical effect.
Acknowledged mutations retain compact UUID tombstones. Pending mutation outcomes and unresolved physical dispatch records remain durable.

Stop the runner with Ctrl+C. The lamp keeps its current state.
A crash after dispatch leaves the uncertainty in the journal. Keep that journal and use an authorized confirmed read after restart.
Do not delete or reuse it to bypass unresolved work.
Repeated connection or acknowledgement failures produce one plain operator message without tokens, addresses, names, or raw provider errors.

## Retired local ingress

The Windows guest proxy, Wi-Fi attachment producer, browser handoff, local TLS server, and reachability diagnostic are superseded.
They are not part of the active entry point and require no setup on a guest-side computer.
Their imported components remain available for isolated historical callers and synthetic regression tests.
The production website factory exposes the outbound rendezvous and same-origin controls.
The old `pilot.example.json` and `guest.example.json` are legacy fixtures, not current setup instructions.

Retained tests cover exact Kasa identity, zero SDK retries, target-bound journals, unknown outcomes, and old adapter isolation.
Outbound tests replace the active ingress scenarios with source-generation rotation, stale/offline denial, fresh dispatch authorization, and persistent result recovery.
The website integration must merge the exact reviewed PJ-60 head before its combined acceptance check.

## Remaining live checks

The hosted website's direct-source contract, real registration mail, owner sign-in, and public guest route still require private integration checks.
Verify automatic public-source refresh and guest matching against the actual ISP and host.
Then run the separately approved phone and selected-lamp acceptance test through the ordinary website bookmark.
No deployment, provider call, router change, guest isolation change, or physical switch forms part of these synthetic tests.

The pilot extra pins python-kasa 0.12.0 (GPL-3.0-or-later) and aiohttp 3.14.5 (Apache-2.0 AND MIT).
Their source is not copied into this repository. The project source remains under its MIT license.


## Synthetic command latency

Run `python demos/command_latency.py --baseline d283d02 --repeats 3` for a local comparison.
It exercises the real hosted queue, journal, authorization, and unchanged home runner with a simulated device and transport.
Both versions use the same request latency, device latency, and poll phases.

The idle case has ready controls but no prior confirmed device state. The warm case has a fresh confirmed read.
This measures tap-to-simulated-actuation and confirmation. It does not measure browser rendering, WAN latency, or installed hardware.
The JSON evidence includes each sample and request counts under `data/pj88-latency/measurement.json`.

The approved, home-ready access response advertises `direct_commands: true`.
The updated client then sends the requested command directly. Older servers without this capability keep the explicit read-then-command path.
The PJ88 comparison did not include automatic reads. The current client keeps an unknown result blocked until an explicit confirmed refresh or a newer confirmed automatic observation.
It never retries an uncertain command. The automatic-update measurements above include read contention separately.


The three paired samples against `d283d02` produced these tap-to-simulated-actuation medians:

| Ready-control state | Baseline | Revised |
| --- | ---: | ---: |
| No prior confirmed device state | 1641 ms | 635 ms |
| Fresh confirmed state | 639 ms | 634 ms |

Both cases used 50 ms simulated website calls, 50 ms device queries, and identical 1-second home and UI poll schedules.
The no-prior-state case removed one queue cycle: two guest operations became one, and four authorizations became two.
Device updates fell from five to three. The remaining three include the command's device refresh and confirmed readback.
Warm-path ranges overlap, so these samples do not establish a warm-path improvement.
This change does not reduce the 15-second idle heartbeat wait before controls become ready.
