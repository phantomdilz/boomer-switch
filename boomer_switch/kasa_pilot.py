"""Resolve only an explicitly selected device through bounded read-only discovery."""

import asyncio
from contextlib import suppress

from kasa import Discover
from kasa.protocols import BaseProtocol

from .lights import CommandDenied


class SingleAttemptProtocol(BaseProtocol):
    """Disable python-kasa's implicit query retries for the pinned provider version."""

    def __init__(self, protocol):
        super().__init__(transport=protocol._transport)
        self.wrapped = protocol

    async def query(self, request, retry_count=0):
        return await self.wrapped.query(request, retry_count=0)

    async def close(self):
        await self.wrapped.close()


class KasaPilotResolver:
    """Treat the private host as a discovery hint and the exact ID as identity."""

    def __init__(self, light, host, *, credentials=None, discover=None):
        if light.outlet_id is not None:
            raise ValueError("Select one standalone pilot lamp.")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("Configure a private device discovery hint.")
        self.light, self.host, self.credentials = light, host, credentials
        self.discover = discover or Discover.discover_single
        self.device = None

    async def __call__(self, identity):
        if identity != self.light.device_id or self.device is not None:
            raise CommandDenied("Resolve only the selected pilot identity.")
        async with asyncio.timeout(3):
            self.device = await self.discover(self.host, discovery_timeout=1, timeout=1, credentials=self.credentials)
            if self.device is None:
                raise CommandDenied("The selected pilot is unavailable.")
            self.device.protocol = SingleAttemptProtocol(self.device.protocol)
            await self.device.update()
            if self.device.device_id != identity or self.device.children or self.device.device_type.value not in ("plug", "bulb", "dimmer"):
                raise CommandDenied("The selected pilot identity is unavailable.")
            return self.device

    async def close(self):
        device, self.device = self.device, None
        if device is not None:
            with suppress(Exception):
                async with asyncio.timeout(1):
                    await device.disconnect()
