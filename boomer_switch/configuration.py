"""Validate shared home and hosted configuration contracts."""

import re


def service_key(value):
    if (not isinstance(value, str) or len(value) < 32
            or value == "REPLACE_WITH_PRIVATE_RANDOM_SERVICE_KEY"):
        raise ValueError("Configure a private service key of at least 32 characters.")
    return value


def light_metadata(light):
    if (not isinstance(light, dict) or set(light) != {"light_id", "name", "room"}
            or not isinstance(light["light_id"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", light["light_id"])):
        raise ValueError("Use valid guest light metadata.")
    if any(not isinstance(light[key], str) or not 1 <= len(light[key].strip()) <= 120
            or any(ord(char) < 32 for char in light[key]) for key in ("name", "room")):
        raise ValueError("Use guest display names of at most 120 characters without control characters.")
    return light
