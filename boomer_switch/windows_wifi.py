"""Verify the selected Windows wireless attachment without changing network settings."""

import base64
import ctypes as C
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from .guest_reachability import validate_address
from .website_grants import PermissionUnavailable


U32 = C.c_uint32


class SSID(C.Structure):
    _fields_ = [("length", U32), ("value", C.c_ubyte * 32)]


class Association(C.Structure):
    _fields_ = [("ssid", SSID), ("bss_type", U32), ("bssid", C.c_ubyte * 6),
                ("phy_type", U32), ("phy_index", U32), ("signal", U32), ("rx", U32), ("tx", U32)]


class Connection(C.Structure):
    _fields_ = [("state", U32), ("mode", U32), ("profile", C.c_uint16 * 256),
                ("association", Association), ("security", U32 * 4)]


def current_wifi(guid):
    """Read the native WLAN connection for this GUID and release all native resources."""
    if os.name != "nt":
        raise PermissionUnavailable
    api = C.WinDLL("wlanapi.dll")
    pointer = C.c_void_p
    api.WlanOpenHandle.argtypes = [U32, pointer, C.POINTER(U32), C.POINTER(pointer)]
    api.WlanQueryInterface.argtypes = [pointer, pointer, U32, pointer, C.POINTER(U32), C.POINTER(pointer), pointer]
    api.WlanFreeMemory.argtypes = [pointer]
    api.WlanFreeMemory.restype = None
    api.WlanCloseHandle.argtypes = [pointer, pointer]
    for name in ("WlanOpenHandle", "WlanQueryInterface", "WlanCloseHandle"):
        getattr(api, name).restype = U32
    handle, data, version, size = pointer(), pointer(), U32(), U32()
    raw_guid = (C.c_ubyte * 16).from_buffer_copy(uuid.UUID(guid).bytes_le)
    if api.WlanOpenHandle(2, None, C.byref(version), C.byref(handle)):
        raise PermissionUnavailable
    try:
        if api.WlanQueryInterface(handle, C.byref(raw_guid), 7, None, C.byref(size), C.byref(data), None):
            raise PermissionUnavailable
        if not data or size.value != C.sizeof(Connection):
            raise PermissionUnavailable
        value = C.cast(data, C.POINTER(Connection)).contents
        association = value.association
        if (value.state != 1 or association.bss_type != 1 or not 1 <= association.ssid.length <= 32
                or value.security[0] != 1 or value.security[2] not in (6, 7, 8, 9, 11)):
            raise PermissionUnavailable
        return bytes(association.ssid.value[:association.ssid.length]), bytes(association.bssid)
    finally:
        if data:
            api.WlanFreeMemory(data)
        api.WlanCloseHandle(handle, None)


# Values are projected to fixed machine-readable fields. No localized command text is parsed.
SNAPSHOT = r"""
$ErrorActionPreference='Stop'
@{
 adapters=@(Get-NetAdapter -IncludeHidden | ForEach-Object { @{index=$_.ifIndex;guid="$($_.InterfaceGuid)";hardware=$_.HardwareInterface;virtual=$_.Virtual;up=($_.Status -eq 'Up')} })
 addresses=@(Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object { @{index=$_.InterfaceIndex;address=$_.IPAddress;prefix=$_.PrefixLength;preferred=($_.AddressState -eq 'Preferred')} })
 interfaces=@(Get-NetIPInterface -AddressFamily IPv4 | ForEach-Object { @{index=$_.InterfaceIndex;forward=($_.Forwarding -ne 'Disabled');weakReceive=($_.WeakHostReceive -ne 'Disabled');weakSend=($_.WeakHostSend -ne 'Disabled')} })
 routes=@(Get-NetRoute -AddressFamily IPv4 | ForEach-Object { @{index=$_.InterfaceIndex;prefix=$_.DestinationPrefix;next=$_.NextHop} })
 neighbors=@(Get-NetNeighbor -AddressFamily IPv4 | ForEach-Object { @{index=$_.InterfaceIndex;address=$_.IPAddress;state="$($_.State)"} })
} | ConvertTo-Json -Depth 4 -Compress
"""


def network_snapshot():
    if os.name != "nt":
        raise PermissionUnavailable
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = base64.b64encode(SNAPSHOT.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
        capture_output=True, timeout=3, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    if len(result.stdout) > 262144:
        raise PermissionUnavailable
    return json.loads(result.stdout.decode("utf-8-sig"))


class WindowsWifi:
    """Require a pinned physical WLAN adapter, access point, address, and direct peer."""

    def __init__(self, *, guid, index, address, prefix, ssid_hex, bssid_hex,
                 wifi=current_wifi, snapshot=network_snapshot, clock=time.time):
        self.guid = str(uuid.UUID(guid))
        if type(index) is not int or index <= 0 or type(prefix) is not int or not 1 <= prefix <= 30:
            raise ValueError("Configure the selected wireless interface and subnet.")
        self.index, self.address = index, validate_address(address)
        self.network = ipaddress.ip_network(f"{self.address}/{prefix}", strict=False)
        self.ssid, self.bssid = bytes.fromhex(ssid_hex), bytes.fromhex(bssid_hex)
        if not 1 <= len(self.ssid) <= 32 or len(self.bssid) != 6:
            raise ValueError("Configure the selected wireless network and access point.")
        self.wifi, self.snapshot, self.clock = wifi, snapshot, clock

    def require(self, peer, local):
        try:
            started = self.clock()
            remote = ipaddress.ip_address(peer)
            if local != self.address or remote not in self.network or remote in (
                    self.network.network_address, self.network.broadcast_address, ipaddress.ip_address(self.address)):
                raise PermissionUnavailable
            state = self.snapshot()
            adapters = [item for item in state["adapters"] if item["index"] == self.index]
            if len(adapters) != 1:
                raise PermissionUnavailable
            adapter = adapters[0]
            if str(uuid.UUID(adapter["guid"])) != self.guid or adapter["hardware"] is not True or adapter["virtual"] is not False or adapter["up"] is not True:
                raise PermissionUnavailable
            addresses = [item for item in state["addresses"] if item["address"] == self.address]
            if len(addresses) != 1 or addresses[0] != {"index": self.index, "address": self.address,
                    "prefix": self.network.prefixlen, "preferred": True}:
                raise PermissionUnavailable
            interfaces = [item for item in state["interfaces"] if item["index"] == self.index]
            if len(interfaces) != 1 or any(item[flag] is not False
                    for item in state["interfaces"] for flag in ("forward", "weakReceive", "weakSend")):
                raise PermissionUnavailable
            if any(item["index"] == self.index and item["next"] == peer for item in state["routes"]):
                raise PermissionUnavailable
            routes = [item for item in state["routes"] if remote in ipaddress.ip_network(item["prefix"])]
            specific = [item for item in routes if ipaddress.ip_network(item["prefix"]).prefixlen >= self.network.prefixlen]
            if not specific or any(item["index"] != self.index or item["next"] != str(ipaddress.IPv4Address(0)) for item in specific):
                raise PermissionUnavailable
            if not any(item["index"] == self.index and item["address"] == peer and item["state"] in ("Reachable", "Stale", "Delay", "Probe") for item in state["neighbors"]):
                raise PermissionUnavailable
            # Query WLAN last, immediately before the caller forwards the accepted request.
            if self.wifi(self.guid) != (self.ssid, self.bssid) or self.clock() >= started + 4:
                raise PermissionUnavailable
            return started + 6
        except Exception:
            raise PermissionUnavailable("The selected guest Wi-Fi attachment is unavailable.") from None
