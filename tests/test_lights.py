import asyncio
from enum import Enum
import json
from pathlib import Path

import pytest

from boomer_switch.lights import CommandDenied, InvalidAllowlist, Light, LightService, LightState, load_allowlist


class DeviceType(Enum):
    Plug = "plug"
    Strip = "strip"
    Unknown = "unknown"


class FakeDevice:
    def __init__(self, device_id="synthetic-device", *, children=None):
        self.device_id = device_id
        self.children = children or []
        self.device_type = DeviceType.Strip if children else DeviceType.Plug
        self.is_on = False
        self.calls = []
        self.update_error = False
        self.command_error = False
        self.ignore_command = False

    async def update(self):
        self.calls.append("update")
        if self.update_error:
            raise OSError("SYNTHETIC_PRIVATE_PROVIDER_DETAIL")

    async def turn_on(self):
        self.calls.append("on")
        if not self.ignore_command:
            self.is_on = True
        if self.command_error:
            raise OSError("SYNTHETIC_PRIVATE_PROVIDER_DETAIL")

    async def turn_off(self):
        self.calls.append("off")
        self.is_on = False


def selected(**changes):
    return Light(**({"light_id": "sample-lamp", "name": "Sample lamp", "room": "Sample room", "device_id": "synthetic-device"} | changes))


def service(device, lights=None):
    resolved = []

    async def resolve(device_id):
        resolved.append(device_id)
        return device

    return LightService((selected(),) if lights is None else lights, resolve), resolved


def test_explicit_standalone_selection_controls_on_and_off():
    device = FakeDevice()
    owner, resolved = service(device)

    async def scenario():
        assert await owner.command("sample-lamp", "on") == LightState.ON
        assert await owner.command("sample-lamp", "off") == LightState.OFF

    asyncio.run(scenario())
    assert resolved == ["synthetic-device", "synthetic-device"]
    assert device.calls == ["update", "on", "update", "update", "off", "update"]


@pytest.mark.parametrize("target,action", [("unselected-light", "on"), ("synthetic-device", "off"), ("sample-lamp", "toggle"), ("sample-lamp", "ON")])
def test_denied_requests_never_resolve_or_touch_a_device(target, action):
    device = FakeDevice()
    owner, resolved = service(device)
    with pytest.raises(CommandDenied):
        asyncio.run(owner.command(target, action))
    assert resolved == []
    assert device.calls == []


def test_empty_allowlist_denies_all_commands_and_reads():
    owner, resolved = service(FakeDevice(), ())
    for operation in (owner.command("sample-lamp", "on"), owner.state("sample-lamp")):
        with pytest.raises(CommandDenied):
            asyncio.run(operation)
    assert resolved == []


def test_only_the_selected_outlet_receives_a_command():
    selected_child = FakeDevice("synthetic-outlet-a")
    sibling = FakeDevice("synthetic-outlet-b")
    parent = FakeDevice(children=[selected_child, sibling])
    owner, resolved = service(parent, (selected(outlet_id="synthetic-outlet-a"),))
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.ON
    assert selected_child.calls == ["on"]
    assert sibling.calls == []
    assert parent.calls == ["update", "update"]
    assert resolved == ["synthetic-device"]


@pytest.mark.parametrize("outlet", [None, "missing-outlet"])
def test_strip_parent_and_missing_outlet_never_receive_commands(outlet):
    child = FakeDevice("synthetic-outlet-a")
    parent = FakeDevice(children=[child])
    owner, _ = service(parent, (selected(outlet_id=outlet),))
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.UNAVAILABLE
    assert parent.calls == ["update"]
    assert child.calls == []


@pytest.mark.parametrize("device_type", [DeviceType.Strip, DeviceType.Unknown])
def test_empty_strip_and_unknown_device_type_are_not_standalone_lights(device_type):
    device = FakeDevice()
    device.device_type = device_type
    owner, _ = service(device)
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.UNAVAILABLE
    assert device.calls == ["update"]


def test_wrong_parent_identity_never_receives_a_command():
    device = FakeDevice("different-synthetic-device")
    owner, _ = service(device)
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.UNAVAILABLE
    assert device.calls == ["update"]


def test_ambiguous_outlet_identity_never_receives_a_command():
    children = [FakeDevice("same-synthetic-outlet"), FakeDevice("same-synthetic-outlet")]
    parent = FakeDevice(children=children)
    owner, _ = service(parent, (selected(outlet_id="same-synthetic-outlet"),))
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.UNAVAILABLE
    assert all(child.calls == [] for child in children)


def test_report_device_state_instead_of_requested_state():
    device = FakeDevice()
    device.ignore_command = True
    owner, _ = service(device)
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.OFF


def test_reads_refresh_device_state_without_mutation():
    device = FakeDevice()
    device.is_on = True
    owner, _ = service(device)
    assert asyncio.run(owner.state("sample-lamp")) == LightState.ON
    assert device.calls == ["update"]


def test_uncertain_command_returns_unavailable_without_retry_or_provider_text():
    device = FakeDevice()
    device.command_error = True
    owner, _ = service(device)
    result = asyncio.run(owner.command("sample-lamp", "on"))
    assert result == LightState.UNAVAILABLE
    assert device.is_on is True
    assert device.calls == ["update", "on"]
    assert "SYNTHETIC_PRIVATE_PROVIDER_DETAIL" not in str(result)


def test_failed_initial_refresh_prevents_mutation():
    device = FakeDevice()
    device.update_error = True
    owner, _ = service(device)
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.UNAVAILABLE
    assert device.calls == ["update"]


@pytest.mark.parametrize("failure", ["refresh", "identity", "removed-outlet"])
def test_final_refresh_failure_cannot_report_confirmed_success(failure):
    child = FakeDevice("synthetic-outlet")
    parent = FakeDevice(children=[child])

    async def update():
        parent.calls.append("update")
        if len(parent.calls) == 2:
            if failure == "refresh":
                raise OSError("SYNTHETIC_PRIVATE_PROVIDER_DETAIL")
            if failure == "identity":
                parent.device_id = "different-synthetic-device"
            else:
                parent.children = []

    parent.update = update
    owner, _ = service(parent, (selected(outlet_id="synthetic-outlet"),))
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.UNAVAILABLE
    assert child.calls == ["on"]


def test_final_refresh_uses_the_current_outlet_state():
    child = FakeDevice("synthetic-outlet")
    parent = FakeDevice(children=[child])

    async def update():
        parent.calls.append("update")
        if len(parent.calls) == 2:
            parent.children = [FakeDevice("synthetic-outlet")]

    parent.update = update
    owner, _ = service(parent, (selected(outlet_id="synthetic-outlet"),))
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.OFF
    assert child.is_on is True


def test_nonboolean_state_is_unavailable():
    device = FakeDevice()
    device.is_on = "unknown"
    owner, _ = service(device)
    assert asyncio.run(owner.state("sample-lamp")) == LightState.UNAVAILABLE


def test_timeout_is_bounded_and_does_not_repeat_resolution():
    calls = []

    async def resolve(device_id):
        calls.append(device_id)
        await asyncio.Event().wait()

    owner = LightService((selected(),), resolve, timeout_seconds=0.01)
    assert asyncio.run(owner.command("sample-lamp", "on")) == LightState.UNAVAILABLE
    assert calls == ["synthetic-device"]


def test_display_fields_do_not_expose_device_or_outlet_identity():
    owner, _ = service(FakeDevice(), (selected(outlet_id="synthetic-private-outlet"),))
    assert owner.lights() == ({"light_id": "sample-lamp", "name": "Sample lamp", "room": "Sample room"},)


def test_sanitized_example_loads():
    entries = load_allowlist(Path(__file__).parents[1] / "devices.example.json")
    assert len(entries) == 2
    assert entries[0].outlet_id is None
    assert entries[1].outlet_id == "OUTLET_ID_PLACEHOLDER"


@pytest.mark.parametrize("document", [
    {}, {"lights": {}}, {"lights": [], "extra": True}, {"lights": [None]},
    {"lights": [{"light_id": "sample-lamp"}]},
    {"lights": [selected().__dict__, selected().__dict__]},
    {"lights": [selected().__dict__, selected(light_id="other-lamp").__dict__]},
    {"lights": [selected().__dict__ | {"device_id": "*"}]},
    {"lights": [selected().__dict__ | {"outlet_id": ""}]},
    {"lights": [selected().__dict__ | {"enabled": True}]},
])
def test_invalid_configuration_fails_closed(tmp_path, document):
    config = tmp_path / "config.local.synthetic.json"
    config.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(InvalidAllowlist):
        load_allowlist(config)


@pytest.mark.parametrize("content", ["not json", '{"lights": [], "lights": []}', '\ufeff{"lights": []}'])
def test_invalid_json_fails_closed(tmp_path, content):
    config = tmp_path / "config.local.synthetic.json"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(InvalidAllowlist):
        load_allowlist(config)


def test_missing_configuration_has_no_discovery_fallback(tmp_path):
    with pytest.raises(InvalidAllowlist):
        load_allowlist(tmp_path / "absent.json")


def test_empty_configuration_explicitly_selects_no_lights(tmp_path):
    config = tmp_path / "config.local.synthetic.json"
    config.write_text('{"lights": []}', encoding="utf-8")
    assert load_allowlist(config) == ()


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan")])
def test_timeout_configuration_cannot_disable_the_bound(timeout):
    with pytest.raises(ValueError):
        LightService((), None, timeout_seconds=timeout)
