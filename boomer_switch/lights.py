"""Enforce the owner's device selection before each device command."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Protocol


class InvalidAllowlist(ValueError):
    """The local allowlist is absent or invalid."""


class CommandDenied(ValueError):
    """The target or action is not allowed."""


class LightState(StrEnum):
    ON = "on"
    OFF = "off"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Light:
    light_id: str
    name: str
    room: str
    device_id: str
    outlet_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.light_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,63}", self.light_id
        ):
            raise InvalidAllowlist("Use a valid light_id.")
        for value in (self.name, self.room, self.device_id):
            if not _explicit(value):
                raise InvalidAllowlist("Use explicit, nonempty light fields.")
        if self.outlet_id is not None and not _explicit(self.outlet_id):
            raise InvalidAllowlist("Use an explicit outlet_id or null.")


def _explicit(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip() and "*" not in value


def load_allowlist(path: Path) -> tuple[Light, ...]:
    """Reject the whole file if any selection is invalid."""
    try:
        def unique_fields(pairs: list[tuple[str, object]]) -> dict:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
        if (
            not isinstance(document, dict)
            or set(document) != {"lights"}
            or not isinstance(document["lights"], list)
        ):
            raise ValueError
        entries = tuple(Light(**entry) for entry in document["lights"])
        _check_duplicates(entries)
        return entries
    except (OSError, UnicodeError, ValueError, TypeError):
        raise InvalidAllowlist("Read a valid local allowlist before control starts.") from None


def _check_duplicates(lights: tuple[Light, ...]) -> None:
    if len({light.light_id for light in lights}) != len(lights):
        raise InvalidAllowlist("Use unique light IDs.")
    if len({(light.device_id, light.outlet_id) for light in lights}) != len(lights):
        raise InvalidAllowlist("Select each device or outlet once.")


class Device(Protocol):
    """The python-kasa device members used by this boundary."""

    @property
    def device_id(self) -> str: ...

    @property
    def device_type(self) -> Enum: ...

    @property
    def children(self) -> list[Device]: ...

    @property
    def is_on(self) -> bool: ...

    async def update(self) -> None: ...
    async def turn_on(self) -> object: ...
    async def turn_off(self) -> object: ...


def _target(parent: Device, light: Light) -> Device | None:
    if parent.device_id != light.device_id:
        return None
    if light.outlet_id is None:
        # A strip parent must never grant control of every outlet.
        if parent.children or parent.device_type.value not in ("plug", "bulb", "dimmer"):
            return None
        return parent
    if parent.device_type.value != "strip":
        return None
    matches = [child for child in parent.children if child.device_id == light.outlet_id]
    if (
        len(matches) != 1
        or matches[0].children
        or matches[0].device_type.value not in ("stripsocket", "plug")
    ):
        return None
    return matches[0]


class LightService:
    """Use a trusted resolver and an immutable owner-selected allowlist."""

    def __init__(
        self,
        lights: tuple[Light, ...],
        resolve: Callable[[str], Awaitable[Device]],
        *,
        timeout_seconds: float = 5,
    ) -> None:
        lights = tuple(lights)
        _check_duplicates(lights)
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Use a timeout above 0 and at most 30 seconds.")
        self._lights = {light.light_id: light for light in lights}
        self._resolve = resolve
        self._timeout = timeout_seconds
        self._locks = {light.device_id: asyncio.Lock() for light in lights}

    def lights(self) -> tuple[dict[str, str], ...]:
        """Return display fields without private device identities."""
        return tuple(
            {"light_id": light.light_id, "name": light.name, "room": light.room}
            for light in self._lights.values()
        )

    async def state(self, light_id: str) -> LightState:
        return await self._operate(light_id, None)

    async def command(self, light_id: str, action: str) -> LightState:
        if action not in ("on", "off"):
            raise CommandDenied("Use on or off.")
        return await self._operate(light_id, action)

    async def _operate(self, light_id: str, action: str | None) -> LightState:
        if not isinstance(light_id, str) or light_id not in self._lights:
            raise CommandDenied("Select an allowed light.")
        light = self._lights[light_id]
        try:
            async with asyncio.timeout(self._timeout), self._locks[light.device_id]:
                parent = await self._resolve(light.device_id)
                await parent.update()
                target = _target(parent, light)
                if target is None:
                    return LightState.UNAVAILABLE
                if action is not None:
                    if action == "on":
                        await target.turn_on()
                    else:
                        await target.turn_off()
                    await parent.update()
                    # Recheck identity after refresh before reporting a state.
                    target = _target(parent, light)
                    if target is None:
                        return LightState.UNAVAILABLE
                observed = target.is_on
                if type(observed) is not bool:
                    return LightState.UNAVAILABLE
                return LightState.ON if observed else LightState.OFF
        except Exception:
            # A failed command can have reached the device. Never retry here.
            return LightState.UNAVAILABLE
