"""Hashing, signing and machine-code helpers for the ETS100 protocol.

The ETS100 API wraps every business call in a signed envelope:

    body  = base64(json([{"r": route, "params": {...}}]))
    sign  = md5(PID + str(timestamp) + body + SECRET_KEY)
    POST  {"body": body, "head": {"version": "1.0", "sign": sign,
                                  "pid": PID, "time": timestamp}}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import random
import re
import subprocess
import uuid
from typing import Any, Dict

from .. import config

# --- basic digests ----------------------------------------------------------


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def b64_encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64_decode(text: str) -> bytes:
    return base64.b64decode(text)


def md5_base64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")


def hmac_sha1_base64(secret: str, text: str) -> str:
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")


def compact_json(data: Any) -> str:
    """JSON without whitespace - the signature depends on the exact bytes."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# --- envelope ---------------------------------------------------------------


def build_envelope(route: str, params: Dict[str, Any], timestamp: int | None = None) -> str:
    """Build the signed request body for ``route``."""
    timestamp = int(timestamp if timestamp is not None else _now())
    body_json = compact_json([{"r": route, "params": params}])
    body = b64_encode(body_json.encode("utf-8"))
    sign = md5_hex(f"{config.ETS_PID}{timestamp}{body}{config.ETS_SECRET_KEY}")
    return compact_json(
        {
            "body": body,
            "head": {
                "version": "1.0",
                "sign": sign,
                "pid": config.ETS_PID,
                "time": timestamp,
            },
        }
    )


def decode_envelope_body(response: Any) -> Any:
    """Unwrap the ``body`` field of an ETS100 response."""
    if isinstance(response, list):
        if not response:
            raise EtsProtocolError("ETS100 接口返回空数组")
        response = response[0]
    if not isinstance(response, dict):
        raise EtsProtocolError("ETS100 接口返回格式不正确")
    return response.get("body")


class EtsProtocolError(RuntimeError):
    """Raised when the server answers with a non-zero business code."""

    def __init__(self, message: str, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


# --- misc helpers ------------------------------------------------------------


def _now() -> float:
    import time

    return time.time()


def create_request_sn() -> str:
    """Client style request id: <unix-seconds><7 random digits>."""
    seconds = int(_now())
    return f"{seconds}{random.randrange(0, 10_000_000):07d}"


def random_hex(length: int) -> str:
    return os.urandom((length + 1) // 2).hex()[:length].upper()


def md5_middle16(text: str) -> str:
    """The ETS100 machine-code flavour of MD5: upper case, chars 8..24."""
    return md5_hex(text).upper()[8:24]


# --- device code -------------------------------------------------------------


def _mac_address() -> str:
    node = uuid.getnode()
    # uuid.getnode() falls back to a random multicast bit when it cannot read
    # the hardware address; that is still stable for the process lifetime and
    # is good enough for a machine-code fingerprint.
    return "".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -8, -8))


def _windows_machine_data() -> str:
    parts = []
    try:
        import winreg  # type: ignore

        for hive, path, value in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", "ProductId"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", "MachineGuid"),
        ):
            try:
                with winreg.OpenKey(hive, path) as key:
                    parts.append(str(winreg.QueryValueEx(key, value)[0]))
            except OSError:
                continue
    except ImportError:
        pass
    return "".join(parts)


def _macos_machine_data() -> str:
    uuid_value = ""
    try:
        output = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
        if match:
            uuid_value = match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return uuid_value


def _linux_machine_data() -> str:
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            continue
    return ""


def machine_data() -> str:
    system = platform.system()
    if system == "Windows":
        return _windows_machine_data()
    if system == "Darwin":
        return _macos_machine_data()
    return _linux_machine_data()


def generate_device_code() -> str:
    """Build the 33 character ETS100 machine code: ``<16 hex>|<16 hex>``.

    The value must stay stable for the lifetime of an installation, otherwise
    the server keeps asking for a device rebind.
    """
    data = machine_data()
    mac = _mac_address()
    if not data:
        # No stable hardware identity available - fall back to a random value
        # that the caller is expected to persist.
        data = random_hex(32)
    return f"{md5_middle16(data)}|{md5_middle16(mac)}"


def looks_like_device_code(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9A-F]{16}\|[0-9A-F]{16}", value or ""))
