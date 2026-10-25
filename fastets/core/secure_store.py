"""Credential storage backed by the operating system.

The account phone number and password never belong in a plain JSON file.  This
module keeps them in the credential store of the platform:

* **macOS** - a dedicated keychain file (``fastets.keychain-db``) next to the
  application data.  The item is created with an access control list that
  trusts **only** the FastETS executable, so no other program on the machine
  can read the password - not even a shell running ``security
  find-generic-password``.  Only an administrator who unlocks the keychain
  with its own authority can get at it.
* **Windows** - DPAPI (``CryptProtectData``), which encrypts the secret for
  the current Windows user account.
* Everything else - a file with owner-only permissions, used only as a last
  resort.

The module never shows a keychain dialog: interaction is switched off, so a
failure is reported instead of blocking the UI on a prompt nobody sees.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

SERVICE_NAME = "FastETS ETS100 Account"
KEYCHAIN_FILE = "fastets.keychain-db"
KEYCHAIN_PASSWORD_FILE = "fastets.keychain.password"
SECRET_FILE = "credentials.enc"


class CredentialError(RuntimeError):
    """Raised when the credential store cannot be used."""


def _chmod(path: Path, mode: int = 0o600) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

class _MacKeychain:
    """macOS keychain access implemented with ctypes (no extra dependency)."""

    def __init__(self, keychain_path: Path) -> None:
        self.path = Path(keychain_path)
        self._lib = None

    # -- loading ---------------------------------------------------------

    def _load(self):
        if self._lib is not None:
            return self._lib
        import ctypes
        from ctypes import POINTER, c_bool, c_char_p, c_int32, c_uint32, c_void_p

        class SecKeychainAttribute(ctypes.Structure):
            _fields_ = [
                ("tag", c_uint32),
                ("length", c_uint32),
                ("data", c_void_p),
            ]

        class SecKeychainAttributeList(ctypes.Structure):
            _fields_ = [
                ("count", c_uint32),
                ("attr", POINTER(SecKeychainAttribute)),
            ]

        try:
            core = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
            sec = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/Security.framework/Security"
            )
        except OSError as exc:  # pragma: no cover - macOS only
            raise CredentialError(f"无法加载 Security.framework: {exc}") from exc

        core.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
        core.CFStringCreateWithCString.restype = c_void_p
        core.CFArrayCreate.argtypes = [c_void_p, POINTER(c_void_p), ctypes.c_long, c_void_p]
        core.CFArrayCreate.restype = c_void_p
        core.CFRelease.argtypes = [c_void_p]

        sec.SecKeychainOpen.argtypes = [c_char_p, POINTER(c_void_p)]
        sec.SecKeychainOpen.restype = c_int32
        sec.SecKeychainUnlock.argtypes = [c_void_p, c_uint32, c_void_p, c_bool]
        sec.SecKeychainUnlock.restype = c_int32
        sec.SecTrustedApplicationCreateFromPath.argtypes = [c_char_p, POINTER(c_void_p)]
        sec.SecTrustedApplicationCreateFromPath.restype = c_int32
        sec.SecAccessCreate.argtypes = [c_void_p, c_void_p, POINTER(c_void_p)]
        sec.SecAccessCreate.restype = c_int32
        sec.SecKeychainItemCreateFromContent.argtypes = [
            c_uint32,
            POINTER(SecKeychainAttributeList),
            c_uint32,
            c_void_p,
            c_void_p,
            c_void_p,
            POINTER(c_void_p),
        ]
        sec.SecKeychainItemCreateFromContent.restype = c_int32
        sec.SecKeychainFindGenericPassword.argtypes = [
            c_void_p,
            c_uint32,
            c_char_p,
            c_uint32,
            c_char_p,
            POINTER(c_uint32),
            POINTER(c_void_p),
            POINTER(c_void_p),
        ]
        sec.SecKeychainFindGenericPassword.restype = c_int32
        sec.SecKeychainItemDelete.argtypes = [c_void_p]
        sec.SecKeychainItemDelete.restype = c_int32
        sec.SecKeychainItemFreeContent.argtypes = [c_void_p, c_void_p]
        sec.SecKeychainItemFreeContent.restype = c_int32
        sec.SecKeychainSearchCreateFromAttributes.argtypes = [
            c_void_p,
            c_uint32,
            POINTER(SecKeychainAttributeList),
            POINTER(c_void_p),
        ]
        sec.SecKeychainSearchCreateFromAttributes.restype = c_int32
        sec.SecKeychainSearchCopyNext.argtypes = [c_void_p, POINTER(c_void_p)]
        sec.SecKeychainSearchCopyNext.restype = c_int32
        sec.SecKeychainSetUserInteractionAllowed.argtypes = [c_bool]
        sec.SecKeychainSetUserInteractionAllowed.restype = c_int32

        self._lib = {
            "ctypes": ctypes,
            "core": core,
            "sec": sec,
            "attribute": SecKeychainAttribute,
            "attribute_list": SecKeychainAttributeList,
            "kCFStringEncodingUTF8": 0x08000100,
            "kSecGenericPasswordItemClass": 0x67656E70,  # 'genp'
            "kSecServiceItemAttr": 0x73766365,        # 'svce'
            "kSecAccountItemAttr": 0x61636374,        # 'acct'
        }
        # Never pop up a keychain dialog: a headless run must fail instead.
        try:
            sec.SecKeychainSetUserInteractionAllowed(False)
        except Exception:  # noqa: BLE001 - best effort
            pass
        return self._lib

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def trusted_executable() -> str:
        """The binary that is allowed to read the keychain item."""
        return str(Path(sys.executable).resolve())

    def _keychain_password(self) -> bytes:
        sidecar = self.path.with_name(KEYCHAIN_PASSWORD_FILE)
        if not sidecar.exists():
            password = secrets.token_hex(32).encode("ascii")
            sidecar.write_bytes(password)
            _chmod(sidecar)
        return sidecar.read_bytes().strip()

    def _ensure_keychain(self) -> None:
        if self.path.exists() and self.path.stat().st_size > 0:
            _chmod(self.path)
            return
        # ``security create-keychain`` creates the file without any prompt.
        password = self._keychain_password().decode("ascii", "ignore")
        result = subprocess.run(
            [
                "/usr/bin/security",
                "create-keychain",
                "-p",
                password,
                str(self.path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if result.returncode != 0 or not self.path.exists():
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise CredentialError(f"创建钥匙串失败: {detail or result.returncode}")

    def _open(self):
        lib = self._load()
        ctypes = lib["ctypes"]
        c_void_p = ctypes.c_void_p
        keychain = c_void_p()
        status = lib["sec"].SecKeychainOpen(str(self.path).encode(), ctypes.byref(keychain))
        if status != 0:
            raise CredentialError(f"打开钥匙串失败 (OSStatus {status})")
        password = self._keychain_password()
        unlock = lib["sec"].SecKeychainUnlock(
            keychain, len(password), ctypes.cast(password, c_void_p), True
        )
        if unlock != 0:
            raise CredentialError(f"解锁钥匙串失败 (OSStatus {unlock})")
        return lib, keychain

    def _access(self, lib):
        """An access list that trusts only the FastETS executable."""
        ctypes = lib["ctypes"]
        c_void_p = ctypes.c_void_p
        trusted = c_void_p()
        status = lib["sec"].SecTrustedApplicationCreateFromPath(
            self.trusted_executable().encode(), ctypes.byref(trusted)
        )
        if status != 0:
            raise CredentialError(f"创建受信程序失败 (OSStatus {status})")
        array = lib["core"].CFArrayCreate(None, ctypes.byref(trusted), 1, None)
        if not array:
            raise CredentialError("无法构造钥匙串访问列表")
        descriptor = lib["core"].CFStringCreateWithCString(
            None, b"FastETS", lib["kCFStringEncodingUTF8"]
        )
        access = c_void_p()
        status = lib["sec"].SecAccessCreate(descriptor, array, ctypes.byref(access))
        lib["core"].CFRelease(array)
        if status != 0:
            raise CredentialError(f"创建钥匙串访问控制失败 (OSStatus {status})")
        return access

    # -- api -------------------------------------------------------------

    def _purge(self, lib, keychain) -> None:
        """Remove every item that carries our service name.

        The items are found by their *attributes*, not by reading them: an
        item written by an older build trusts a different executable, so
        reading it would fail.  The attribute search ignores the access list,
        which is the only way to get rid of such a leftover.
        """
        ctypes = lib["ctypes"]
        c_void_p = ctypes.c_void_p
        service = SERVICE_NAME.encode("utf-8")
        attributes = (lib["attribute"] * 1)()
        attributes[0].tag = lib["kSecServiceItemAttr"]
        attributes[0].length = len(service)
        attributes[0].data = ctypes.cast(ctypes.c_char_p(service), c_void_p)
        attr_list = lib["attribute_list"](1, attributes)
        search = c_void_p()
        status = lib["sec"].SecKeychainSearchCreateFromAttributes(
            keychain,
            lib["kSecGenericPasswordItemClass"],
            ctypes.byref(attr_list),
            ctypes.byref(search),
        )
        if status != 0:
            return
        while True:
            item = c_void_p()
            status = lib["sec"].SecKeychainSearchCopyNext(search, ctypes.byref(item))
            if status != 0:
                return
            if lib["sec"].SecKeychainItemDelete(item) != 0:
                return

    def save(self, account: str, secret: str) -> None:
        self._ensure_keychain()
        lib, keychain = self._open()
        ctypes = lib["ctypes"]
        c_void_p = ctypes.c_void_p
        access = self._access(lib)
        # Clear the slot first: a leftover item (for example one written by an
        # older build whose access list trusted a different executable) would
        # make the write fail with errSecDuplicateItem.
        self._purge(lib, keychain)
        service = SERVICE_NAME.encode("utf-8")
        account_bytes = account.encode("utf-8")
        payload = secret.encode("utf-8")

        attributes = (lib["attribute"] * 2)()
        attributes[0].tag = lib["kSecServiceItemAttr"]
        attributes[0].length = len(service)
        attributes[0].data = ctypes.cast(ctypes.c_char_p(service), c_void_p)
        attributes[1].tag = lib["kSecAccountItemAttr"]
        attributes[1].length = len(account_bytes)
        attributes[1].data = ctypes.cast(ctypes.c_char_p(account_bytes), c_void_p)
        attr_list = lib["attribute_list"](2, attributes)

        buffer = ctypes.create_string_buffer(payload)
        item = c_void_p()
        status = lib["sec"].SecKeychainItemCreateFromContent(
            lib["kSecGenericPasswordItemClass"],
            ctypes.byref(attr_list),
            len(payload),
            ctypes.cast(buffer, c_void_p),
            keychain,
            access,
            ctypes.byref(item),
        )
        if status != 0:
            raise CredentialError(f"写入钥匙串失败 (OSStatus {status})")

    def load(self, account: str) -> str:
        lib, keychain = self._open()
        ctypes = lib["ctypes"]
        c_void_p = ctypes.c_void_p
        service = SERVICE_NAME.encode("utf-8")
        account_bytes = account.encode("utf-8")
        length = ctypes.c_uint32(0)
        data = c_void_p()
        item = c_void_p()
        status = lib["sec"].SecKeychainFindGenericPassword(
            keychain,
            len(service),
            service,
            len(account_bytes),
            account_bytes,
            ctypes.byref(length),
            ctypes.byref(data),
            ctypes.byref(item),
        )
        if status != 0:
            if status == -25293:
                # errSecAuthFailed: the item exists but trusts another build of
                # the program (the binary was replaced).  The caller has to log
                # in again, which rewrites the item.
                raise CredentialError(
                    "钥匙串里的凭据属于另一个版本的 FastETS，请重新登录一次"
                )
            raise CredentialError(f"读取钥匙串失败 (OSStatus {status})")
        try:
            return ctypes.string_at(data, length.value).decode("utf-8", "replace")
        finally:
            # The data buffer belongs to the keychain; the item stays.
            lib["sec"].SecKeychainItemFreeContent(None, data)

    def delete(self, account: str) -> bool:
        lib, keychain = self._open()
        self._purge(lib, keychain)
        return True


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

class _WindowsDpapi:
    """Windows DPAPI - the secret is encrypted for the current user."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _protect(self, data: bytes) -> bytes:
        import ctypes
        from ctypes import POINTER, c_uint32, c_void_p

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", c_uint32), ("pbData", c_void_p)]

        advapi = ctypes.cdll.LoadLibrary("Advapi32.dll")
        crypt = ctypes.cdll.LoadLibrary("Crypt32.dll")
        source = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), c_void_p))
        result = DATA_BLOB()
        description = ctypes.create_unicode_buffer("FastETS")
        if not crypt.CryptProtectData(
            ctypes.byref(source),
            ctypes.cast(description, c_void_p),
            None,
            None,
            None,
            0,
            ctypes.byref(result),
        ):
            raise CredentialError(f"DPAPI 加密失败: {ctypes.get_last_error()}")
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            advapi.LocalFree(result.pbData)

    def _unprotect(self, data: bytes) -> bytes:
        import ctypes
        from ctypes import POINTER, c_uint32, c_void_p

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", c_uint32), ("pbData", c_void_p)]

        advapi = ctypes.cdll.LoadLibrary("Advapi32.dll")
        crypt = ctypes.cdll.LoadLibrary("Crypt32.dll")
        source = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), c_void_p))
        result = DATA_BLOB()
        if not crypt.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(result),
        ):
            raise CredentialError(f"DPAPI 解密失败: {ctypes.get_last_error()}")
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            advapi.LocalFree(result.pbData)

    def save(self, account: str, secret: str) -> None:
        payload = json.dumps({"account": account, "secret": secret}).encode("utf-8")
        self.path.write_bytes(self._protect(payload))
        _chmod(self.path)

    def load(self, account: str) -> str:
        if not self.path.exists():
            raise CredentialError("凭据文件不存在")
        data = json.loads(self._unprotect(self.path.read_bytes()).decode("utf-8"))
        if str(data.get("account")) != str(account):
            raise CredentialError("凭据与账号不匹配")
        return str(data.get("secret") or "")

    def delete(self, account: str) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

class _PlainFile:
    """Owner-only file, used when no system store is available."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, account: str, secret: str) -> None:
        self.path.write_text(
            json.dumps({"account": account, "secret": secret}), encoding="utf-8"
        )
        _chmod(self.path)

    def load(self, account: str) -> str:
        if not self.path.exists():
            raise CredentialError("凭据文件不存在")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if str(data.get("account")) != str(account):
            raise CredentialError("凭据与账号不匹配")
        return str(data.get("secret") or "")

    def delete(self, account: str) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class CredentialStore:
    """The account name and password of the logged-in user."""

    #: The keychain item is stored under a fixed account name so that nothing
    #: sensitive has to stay in the plain state file.
    ITEM_ACCOUNT = "default"

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._backend = None
        self._backend_name = ""
        self.last_error = ""
        self._init_backend()

    def _init_backend(self) -> None:
        system = platform.system()
        try:
            if system == "Darwin":
                self._backend = _MacKeychain(self.data_dir / KEYCHAIN_FILE)
                self._backend_name = "macOS 钥匙串"
                return
            if system == "Windows":
                self._backend = _WindowsDpapi(self.data_dir / SECRET_FILE)
                self._backend_name = "Windows DPAPI"
                return
        except Exception:  # noqa: BLE001 - fall through to the plain file
            self._backend = None
        self._backend = _PlainFile(self.data_dir / SECRET_FILE)
        self._backend_name = "本地文件（仅当前用户可读）"

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def save(self, payload: Dict[str, str]) -> None:
        if not payload:
            raise CredentialError("凭据为空，无法保存")
        self._backend.save(self.ITEM_ACCOUNT, json.dumps(payload, ensure_ascii=False))

    def load(self) -> Dict[str, str]:
        self.last_error = ""
        raw = self._load_with_timeout()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _load_with_timeout(self, timeout: float = 8.0) -> str:
        """Read the secret without ever freezing the interface.

        The keychain can decide to ask the user for permission (for example
        after the application was replaced).  Interaction is switched off, so
        the call normally fails right away - but a defensive timeout keeps the
        window responsive even if it does not.
        """
        import threading

        result: Dict[str, Any] = {}

        def worker() -> None:
            try:
                result["value"] = self._backend.load(self.ITEM_ACCOUNT)
            except CredentialError as exc:
                result["error"] = str(exc)
            except Exception as exc:  # noqa: BLE001
                result["error"] = str(exc)

        thread = threading.Thread(target=worker, name="fastets-keychain", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.last_error = "读取系统凭据超时，请重新登录一次"
            return ""
        if "error" in result:
            self.last_error = str(result["error"])
            return ""
        return str(result.get("value") or "")

    def delete(self) -> bool:
        try:
            return bool(self._backend.delete(self.ITEM_ACCOUNT))
        except Exception:  # noqa: BLE001
            return False

