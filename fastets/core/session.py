"""Authentication state: login flows, device code and account selection."""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from .. import config
from . import crypto
from .ets_api import (
    DeviceBindRequired,
    EtsApiClient,
    EtsApiError,
    PROTOCOL_CAPTURED,
    PROTOCOL_LEGACY,
)

STATE_KEY = "session"


class Session:
    """Persisted credentials plus the live API client."""

    def __init__(self, logger=None) -> None:
        self._log = logger
        self._lock = threading.RLock()
        self._store = config.JsonStore(config.state_path(), {})
        self._data: Dict[str, Any] = self._store.read().get(STATE_KEY) or {}
        self._client: Optional[EtsApiClient] = None

    # -- persistence --------------------------------------------------------

    def _save(self) -> None:
        def mutator(root: Dict[str, Any]) -> Dict[str, Any]:
            root = dict(root or {})
            root[STATE_KEY] = self._data
            return root

        self._store.update(mutator)

    def _read(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    # -- device code ---------------------------------------------------------

    @property
    def device_code(self) -> str:
        with self._lock:
            code = self._data.get("device_code") or ""
            if not crypto.looks_like_device_code(code):
                code = crypto.generate_device_code()
                self._data["device_code"] = code
                self._save()
            return code

    # -- credentials ---------------------------------------------------------

    @property
    def token(self) -> str:
        return str(self._read("token", "") or "")

    @property
    def phone(self) -> str:
        return str(self._read("phone", "") or "")

    @property
    def password(self) -> str:
        return str(self._read("password", "") or "")

    @property
    def parent_account_id(self) -> str:
        return str(self._read("parent_account_id", "") or "")

    @property
    def selected_ecard_id(self) -> str:
        return str(self._read("selected_ecard_id", "") or "")

    @property
    def protocol(self) -> str:
        return str(self._read("protocol", PROTOCOL_LEGACY) or PROTOCOL_LEGACY)

    @property
    def is_logged_in(self) -> bool:
        return bool(self.token)

    def remember(self, **fields: Any) -> None:
        with self._lock:
            self._data.update({k: v for k, v in fields.items() if v is not None})
            self._save()

    def clear(self) -> None:
        with self._lock:
            keep = {"device_code": self._data.get("device_code")}
            self._data = keep
            self._save()

    # -- client --------------------------------------------------------------

    @property
    def client(self) -> EtsApiClient:
        with self._lock:
            if self._client is None:
                settings = config.load_settings()
                self._client = EtsApiClient(
                    protocol=self.protocol,
                    verify_ssl=not settings.get("skip_ssl_verify", False),
                    logger=self._log,
                )
            return self._client

    def set_protocol(self, protocol: str) -> None:
        with self._lock:
            self._data["protocol"] = protocol
            self._save()
            self._client = None

    # -- login flows ---------------------------------------------------------

    def authenticate(
        self,
        phone: str,
        password: str,
        remember_password: bool = True,
    ) -> Dict[str, Any]:
        """Log in and return the token plus every E-card of that account.

        Does **not** pick an account - the caller decides which one to use so
        the UI can ask the user.
        """
        client = self.client
        try:
            payload = client.login(phone, password, self.device_code)
        except DeviceBindRequired:
            payload = client.bind_device(phone, password, self.device_code)

        body = payload.get("body") or {}
        token = str(body.get("token", ""))
        if not token:
            raise EtsApiError("登录成功但服务端未返回 token", route="user/login")

        accounts = client.ecard_list(token)
        usable = [item for item in accounts if client.is_usable_account(item)]

        remembered_id = self.selected_ecard_id
        for account in accounts:
            if account["id"] == remembered_id and client.is_usable_account(account):
                usable = [account]
                break

        self._pending_token = token
        self._pending_phone = phone
        self._pending_password = password if remember_password else None
        self._pending_remember = remember_password
        self._usable_accounts = usable
        return {"token": token, "accounts": accounts, "usable": usable}

    def activate_account(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """Persist the token and the chosen E-card."""
        token = getattr(self, "_pending_token", "") or self.token
        if not token:
            raise EtsApiError("登录状态已丢失，请重新登录")
        self.remember(
            phone=getattr(self, "_pending_phone", "") or self.phone,
            password=getattr(self, "_pending_password", None),
            token=token,
            parent_account_id=account["parent_id"],
            selected_ecard_id=account["id"],
            selected_ecard_name=account.get("name", ""),
            selected_ecard_grade=account.get("grade", ""),
            selected_ecard_class_id=account.get("class_id", ""),
        )
        return {"token": token, "account": account}

    def login_with_password(
        self,
        phone: str,
        password: str,
        remember_password: bool = True,
        on_accounts: Optional[Callable[[List[Dict[str, Any]]], str]] = None,
    ) -> Dict[str, Any]:
        """Log in, resolve the parent account and persist the session.

        ``on_accounts`` is called with the usable E-card accounts when more than
        one is available; it must return the ``parent_id`` to use.
        """
        result = self.authenticate(phone, password, remember_password)
        usable = result["usable"]
        if not usable:
            raise EtsApiError(
                "该账号下没有可用年级/班级（E卡可能已过期），请先在官方 App 续费或激活"
            )
        if len(usable) == 1:
            selected = usable[0]
        else:
            if on_accounts is None:
                raise EtsApiError(
                    "该账号下有多个可用年级/班级，请指定要使用哪一个: "
                    + ", ".join(f"{a['grade']}{a['class_name']}" for a in usable)
                )
            parent_id = on_accounts(usable)
            selected = next(
                (
                    account
                    for account in usable
                    if account["parent_id"] == parent_id or account["id"] == parent_id
                ),
                None,
            )
            if selected is None:
                raise EtsApiError("选定的账号无效")
        self.activate_account(selected)
        return {
            "token": result["token"],
            "parent_account_id": selected["parent_id"],
            "account": selected,
            "accounts": result["accounts"],
        }

    def login_with_changyan(
        self, uid: str, captcha_result: str, on_accounts=None
    ) -> Dict[str, Any]:
        """Log in using a 畅言 passport session (uid + captchaResult)."""
        client = self.client
        payload = client.login_zt(uid, uid, captcha_result, self.device_code)
        body = payload.get("body") or {}
        token = str(body.get("token", ""))
        if not token:
            raise EtsApiError("畅言登录成功但服务端未返回 token", route="user/login-zt")

        accounts = client.ecard_list(token)
        usable = [item for item in accounts if client.is_usable_account(item)]
        if not usable:
            raise EtsApiError("畅言账号下没有可用年级/班级")

        self._pending_token = token
        self._usable_accounts = usable
        if len(usable) == 1:
            self.activate_account(usable[0])
            return {
                "token": token,
                "parent_account_id": usable[0]["parent_id"],
                "account": usable[0],
                "accounts": accounts,
            }
        if on_accounts is None:
            return {
                "token": token,
                "parent_account_id": "",
                "account": {},
                "accounts": accounts,
                "usable": usable,
            }
        parent_id = on_accounts(usable)
        selected = next(
            (
                account
                for account in usable
                if account["parent_id"] == parent_id or account["id"] == parent_id
            ),
            None,
        )
        if selected is None:
            raise EtsApiError("选定的账号无效")
        self.activate_account(selected)
        return {
            "token": token,
            "parent_account_id": selected["parent_id"],
            "account": selected,
            "accounts": accounts,
        }

    def import_token(self, token: str) -> Dict[str, Any]:
        """Use a token captured from the official client."""
        client = self.client
        accounts = client.ecard_list(token)
        usable = [item for item in accounts if client.is_usable_account(item)]
        if not usable:
            raise EtsApiError("该 Token 没有可用账号，可能已过期或未绑定设备")
        selected = usable[0]
        self._pending_token = token
        self._usable_accounts = usable
        self.remember(
            token=token,
            parent_account_id=selected["parent_id"],
            selected_ecard_id=selected["id"],
            selected_ecard_name=selected.get("name", ""),
            selected_ecard_grade=selected.get("grade", ""),
            selected_ecard_class_id=selected.get("class_id", ""),
        )
        return {"token": token, "parent_account_id": selected["parent_id"], "account": selected}

    def ensure_fresh_token(self) -> str:
        """Re-login when the stored token has expired."""
        if self.token:
            return self.token
        if self.phone and self.password:
            self.login_with_password(self.phone, self.password)
            return self.token
        raise EtsApiError("尚未登录，请先登录账号")
