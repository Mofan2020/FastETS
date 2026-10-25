"""HTTP client for the E听说 (ets100) cloud API.

Every route listed here was recovered from the official client's wire format
and from the open-source re-implementations of it. See ``docs/API.md`` for the
raw notes.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from .. import config
from . import crypto

# Routes ---------------------------------------------------------------------
ROUTE_LOGIN = "user/login"
ROUTE_REBIND = "user/rebind-code"
ROUTE_LOGIN_ZT = "user/login-zt"
ROUTE_ECARD_LIST = "m/ecard/list"
ROUTE_HOMEWORK_LIST = "g/homework/list"
ROUTE_OSS_CONFIG = "p1/student/config/config/ossEvaluateUploadStsToken"
ROUTE_AUDIO_SYNC = "m/audio/sync-v2"

# Business error codes we act on --------------------------------------------
CODE_DEVICE_BIND_REQUIRED = 30014
CODE_NO_VALID_CLASS = 80004
CODE_TOKEN_EXPIRED = 20004

# Homework list filters ------------------------------------------------------
STATUS_TODO = "1"      # 未完成
STATUS_DOING = "2"     # 已提交待批改 / 进行中
STATUS_DONE = "3"      # 已完成

PROTOCOL_LEGACY = "grlx-native"
PROTOCOL_CAPTURED = "grlx-imported-token"


class EtsApiError(RuntimeError):
    """Business error returned by the ETS100 API."""

    def __init__(self, message: str, code: Any = None, route: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.route = route


# Secrets that must never reach the log file ---------------------------------
_SECRET_PARAMS = ("password", "token", "code", "suc_user_token", "login")


def redact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Copy the request parameters with every secret masked.

    The debug log used to write the plain login password into
    ``fastets.log``, which is a real leak: the log is a plain text file in the
    user's data directory.
    """
    redacted: Dict[str, Any] = {}
    for key, value in (params or {}).items():
        if key not in _SECRET_PARAMS:
            redacted[key] = value
        elif key == "token" and value:
            redacted[key] = f"{str(value)[:6]}***"
        else:
            redacted[key] = "***"
    return redacted


class DeviceBindRequired(EtsApiError):
    """The server wants this device to be (re)bound before it issues a token."""

    def __init__(self, message: str = "设备需要绑定验证", code: Any = None) -> None:
        super().__init__(message, code, ROUTE_LOGIN)


class EtsApiClient:
    """Thin, synchronous wrapper around the signed ETS100 envelope."""

    def __init__(
        self,
        protocol: str = PROTOCOL_LEGACY,
        timeout: int = 30,
        verify_ssl: bool = True,
        logger=None,
        token_refresher=None,
    ) -> None:
        self.protocol = protocol
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self._log = logger
        # Optional callback that returns a fresh token.  ETS tokens expire after
        # a while; without this the whole run dies with "ets的token过期" even
        # though the account is fine.
        self._token_refresher = token_refresher
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": config.ETS_USER_AGENT, "Accept": "*/*"}
        )

    # -- plumbing ----------------------------------------------------------

    def _log_info(self, message: str, *args: Any) -> None:
        if self._log:
            self._log.info(message, *args)

    def _log_debug(self, message: str, *args: Any) -> None:
        if self._log:
            self._log.debug(message, *args)

    def request(
        self,
        route: str,
        params: Dict[str, Any],
        allow_codes: Optional[List[int]] = None,
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """POST a signed envelope to ``route`` and return the parsed response."""
        payload = self._raw_request(route, params, query)
        refreshable = route not in (ROUTE_LOGIN, ROUTE_REBIND) and bool(params.get("token"))
        if refreshable and self._is_token_expired(payload) and self._token_refresher is not None:
            fresh = ""
            try:
                fresh = str(self._token_refresher() or "")
            except Exception as exc:  # noqa: BLE001 - refresh is best effort
                self._log_info("token 刷新失败: %s", exc)
            if fresh:
                self._log_info("token 已过期，自动重新登录后重试 /%s", route)
                params = dict(params)
                params["token"] = fresh
                payload = self._raw_request(route, params, query)
        return self._check_payload(payload, route, allow_codes)

    @staticmethod
    def _is_token_expired(payload: Dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        try:
            code = int(payload.get("code") or 0)
        except (TypeError, ValueError):
            code = 0
        if code == CODE_TOKEN_EXPIRED:
            return True
        message = str(payload.get("msg") or "")
        return "token过期" in message or "登录状态失效" in message

    def _raw_request(
        self,
        route: str,
        params: Dict[str, Any],
        query: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        envelope = crypto.build_envelope(route, params)
        url = f"{config.ETS_API_BASE_URL}/{route}"
        if query:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(query)}"

        self._log_info("ETS POST /%s", route)
        self._log_debug("ETS params /%s: %s", route, redact_params(params))
        try:
            response = self.session.post(
                url,
                data=envelope.encode("utf-8"),
                headers={
                    "Host": "api.ets100.com",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
        except requests.RequestException as exc:
            raise EtsApiError(f"网络请求失败 /{route}: {exc}", route=route) from exc

        if response.status_code >= 400:
            raise EtsApiError(
                f"HTTP {response.status_code} /{route}: {response.text[:300]}",
                route=route,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise EtsApiError(
                f"/{route} 返回了非 JSON 内容: {response.text[:200]}", route=route
            ) from exc

        if isinstance(payload, list):
            if not payload:
                raise EtsApiError(f"/{route} 返回空数组", route=route)
            payload = payload[0]
        if not isinstance(payload, dict):
            raise EtsApiError(f"/{route} 返回格式不正确", route=route)
        return payload

    def _check_payload(
        self,
        payload: Dict[str, Any],
        route: str,
        allow_codes: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        code = payload.get("code")
        if code not in (None, "", 0, "0"):
            message = str(payload.get("msg") or f"业务错误 code={code}")
            if allow_codes and int(code) in allow_codes:
                return payload
            raise EtsApiError(message, code, route)
        return payload

    # -- shared parameter helpers ------------------------------------------

    def _legacy_params(self, token: str) -> Dict[str, Any]:
        return {
            "sn": config.ETS_DEFAULT_SN,
            "token": token,
            "version": config.ETS_DEFAULT_VERSION,
            "system": config.ETS_DEFAULT_SYSTEM,
            "global_client_version": config.ETS_DEFAULT_GLOBAL_CLIENT_VERSION,
            "sign_response": 1,
        }

    def _captured_params(self, token: str, sn: str) -> Dict[str, Any]:
        return {
            "sn": sn,
            "token": token,
            "system": config.ETS_CAPTURED_SYSTEM,
            "is_zk_pad": 0,
            "global_client_version": config.ETS_CAPTURED_APP_VERSION,
            "use_https": 1,
            "use_homework_privilege": 1,
            "ets_app": 0,
            "sign_response": 2,
        }

    def _common(self, token: str) -> Dict[str, Any]:
        if self.protocol == PROTOCOL_CAPTURED:
            return self._captured_params(token, crypto.create_request_sn())
        return self._legacy_params(token)

    # -- authentication -----------------------------------------------------

    def login(self, phone: str, password: str, device_code: str) -> Dict[str, Any]:
        """Log in with phone + password.

        Returns the raw response. Raises :class:`DeviceBindRequired` when the
        server answers ``code=30014``.
        """
        params = {
            "sn": config.ETS_DEFAULT_SN,
            "phone": phone,
            "password": password,
            "device_code": device_code,
            "device_name": config.ETS_DEFAULT_DEVICE_NAME,
            "version": config.ETS_DEFAULT_VERSION,
            "local_ip": config.ETS_DEFAULT_LOCAL_IP,
            "system": config.ETS_DEFAULT_SYSTEM,
            "global_client_version": config.ETS_DEFAULT_GLOBAL_CLIENT_VERSION,
            "sign_response": 1,
        }
        payload = self.request(
            ROUTE_LOGIN, params, allow_codes=[CODE_DEVICE_BIND_REQUIRED]
        )
        if int(payload.get("code") or 0) == CODE_DEVICE_BIND_REQUIRED:
            raise DeviceBindRequired(
                str(payload.get("msg") or "设备需要绑定验证"),
                payload.get("code"),
            )
        return payload

    def bind_device(self, phone: str, password: str, device_code: str) -> Dict[str, Any]:
        """Bind this machine and receive a token in the same call."""
        params = {
            "sn": config.ETS_DEFAULT_SN,
            "phone": phone,
            "email": "",
            "password": password,
            "code": "0",
            "version": config.ETS_DEFAULT_REBIND_VERSION,
            "device_name": config.ETS_DEFAULT_REBIND_DEVICE_NAME,
            "device_code": device_code,
            "local_ip": config.ETS_DEFAULT_LOCAL_IP,
            "system": config.ETS_DEFAULT_SYSTEM,
            "global_client_version": config.ETS_DEFAULT_GLOBAL_CLIENT_VERSION,
            "sign_response": 1,
        }
        return self.request(ROUTE_REBIND, params)

    def refresh_token(self, token: str) -> Dict[str, Any]:
        """Lightweight token refresh (``/user/rebind-code``)."""
        params = {"token": token, "version": "2", "system": config.ETS_DEFAULT_SYSTEM}
        return self.request(ROUTE_REBIND, params)

    def login_zt(
        self, login: str, user_id: str, suc_user_token: str, device_code: str
    ) -> Dict[str, Any]:
        """Log in with a 畅言 (changyan) passport session."""
        params = {
            "sn": config.ETS_DEFAULT_SN,
            "login": login,
            "user_id": user_id,
            "suc_user_token": suc_user_token,
            "device_code": device_code,
            "device_name": "Android",
            "suc_device_name": "Other",
            "os_type": "android",
            "system": config.ETS_DEFAULT_SYSTEM,
            "global_client_version": "",
            "sign_response": 1,
        }
        return self.request(ROUTE_LOGIN_ZT, params)

    # -- accounts -----------------------------------------------------------

    def ecard_list(self, token: str) -> List[Dict[str, Any]]:
        """Return every E-card (grade/class) account attached to ``token``."""
        params = self._common(token)
        params["version"] = config.ETS_DEFAULT_VERSION
        payload = self.request(ROUTE_ECARD_LIST, params)
        body = payload.get("body") or {}
        if not isinstance(body, dict):
            return []
        accounts: List[Dict[str, Any]] = []
        for key, value in body.items():
            if not isinstance(value, dict):
                continue
            accounts.append(
                {
                    "key": key,
                    "id": str(value.get("id", "")),
                    "parent_id": str(value.get("parent_id", "")),
                    "user_account_id": str(value.get("user_account_id", "")),
                    "name": str(value.get("name", "")),
                    "grade": str(value.get("grade", "")),
                    "class_name": str(value.get("class_name", "")),
                    "class_id": str(value.get("class_id", "")),
                    "status": str(value.get("status", "")),
                    "out_of_date": str(value.get("out_of_date", "")),
                    "mobile_status": str(value.get("mobile_status", "")),
                    "raw": value,
                }
            )
        return accounts

    @staticmethod
    def is_usable_account(account: Dict[str, Any]) -> bool:
        """An account is usable when it is active, unexpired and bound to a class."""
        return (
            account.get("status") == "0"
            and account.get("out_of_date") == "0"
            and bool(account.get("class_id"))
            and account.get("mobile_status") == "1"
        )

    # -- homework -----------------------------------------------------------

    def homework_list(
        self,
        token: str,
        parent_account_id: str,
        status: str = STATUS_TODO,
        include_sets: bool = True,
    ) -> Dict[str, Any]:
        """List assignments.

        ``status`` is ``1`` 未完成, ``2`` 进行中, ``3`` 已完成.
        ``include_sets`` adds the flags the official client uses to also return
        作业集 (homework sets) and their child papers.
        """
        params = self._common(token)
        params.update(
            {
                "parent_account_id": parent_account_id,
                "limit": 0,
                "status": status,
                "offset": 0,
                "max_end_time": "",
                "max_homework_id": "",
                "min_end_time": "",
                "min_homework_id": "",
                "get_to_do_count": 1,
                "show_old_homework": 1,
                "parent_homework_id": "",
                "get_all_count": 1,
                "check_pass": 1,
                "get_to_overtime_count": 1,
            }
        )
        if self.protocol == PROTOCOL_CAPTURED:
            params.update({"show_offline": 1, "only_parent": 1, "get_audio_task": 1})
            params["version"] = 10
        payload = self.request(ROUTE_HOMEWORK_LIST, params)
        body = payload.get("body") or {}
        if not isinstance(body, dict):
            body = {}
        homeworks: List[Dict[str, Any]] = []
        for row in body.get("data") or []:
            if not isinstance(row, dict):
                continue
            struct = row.get("struct") or {}
            contents = struct.get("contents") if isinstance(struct, dict) else []
            homeworks.append(
                {
                    "id": str(row.get("id", "")),
                    "name": str(row.get("name", "未命名作业")),
                    "status": status,
                    "set_id": str(row.get("set_id", "")),
                    # The server looks the marking service up by resource_id.
                    # Sending the wrong one (or the generic default) makes every
                    # submission fail with "远程获取ets服务失败".
                    "resource_id": str(row.get("resource_id", "")),
                    "parent_homework_id": str(row.get("parent_homework_id", "")),
                    "group_name": str(row.get("group_name", "")),
                    "contents": [
                        {
                            "group_name": str(
                                entry.get("group_name") or entry.get("groupName") or ""
                            ),
                            "url": str(entry.get("url") or ""),
                            "real_id": str(entry.get("real_id", "")),
                            "entity_type": str(entry.get("entity_type", "")),
                            "read_type": str(entry.get("read_type", "")),
                        }
                        for entry in (contents or [])
                        if isinstance(entry, dict)
                    ],
                    "raw": row,
                }
            )
        return {
            "base_url": str(body.get("base_url") or config.ETS_CDN_BASE_URL),
            "status": status,
            "homeworks": homeworks,
            "total_count": _as_int(body.get("all_count"), len(homeworks)),
            "todo_count": _as_int(body.get("to_do_count"), 0),
            "overtime_count": _as_int(
                body.get("to_over_time_count", body.get("to_overtime_count")), 0
            ),
        }

    # -- upload + submission -------------------------------------------------

    def oss_upload_config(self, token: str) -> Dict[str, Any]:
        """Fetch short-lived Aliyun OSS credentials for the answer upload."""
        sn = crypto.create_request_sn()
        params = {
            "system": config.ETS_CAPTURED_SYSTEM,
            "is_zk_pad": 0,
            "global_client_version": config.ETS_CAPTURED_APP_VERSION,
            "sn": sn,
            "token": token,
            "use_https": 1,
            "use_homework_privilege": 1,
            "ets_app": 0,
            "sign_response": 2,
        }
        payload = self.request(ROUTE_OSS_CONFIG, params)
        body = payload.get("body") or {}
        upload = body.get("upload") if isinstance(body, dict) else {}
        upload = upload if isinstance(upload, dict) else {}
        return {
            "security_token": str(upload.get("securityToken", "")),
            "access_key_id": str(upload.get("accessKeyId", "")),
            "access_key_secret": str(upload.get("accessKeySecret", "")),
            "bucket": str(upload.get("bucket", "")),
            "endpoint": str(upload.get("endpoint", "")),
            "prefix": str(upload.get("prefix", "https://")),
            "cname": str(upload.get("cname", "")),
            "expiration": str(upload.get("expiration", "")),
            "region": str(upload.get("region", "")),
            "mp3_path": str(body.get("mp3Path", "")),
            "spx_path": str(body.get("spxPath", "")),
            "xml_path": str(body.get("xmlPath", "")),
            "timeout": _as_int(body.get("timeout"), 0),
        }

    def submit_audio(
        self,
        token: str,
        context: Dict[str, Any],
        score: str,
        score_detail: str,
        mp3_url: str,
        speex_url: str,
        xml_url: str,
    ) -> Dict[str, Any]:
        """Submit an evaluated answer (audio + score detail) back to ETS100."""
        sn = crypto.create_request_sn()
        params = {
            "homework_id": context.get("homework_id", ""),
            "use_time": context.get("use_time", 0),
            "resource_id": context.get("resource_id", config.ETS_EVALUATE_RESOURCE_ID),
            "serial_id": context.get("serial_id", "0"),
            "set_id": context.get("set_id", ""),
            "entity_id": context.get("entity_id", ""),
            "order": context.get("order", ""),
            "score": score,
            "correct": context.get("correct", 0),
            "auto_submit": 1,
            "client_time": _client_time(),
            "version": 2,
            "graduation": 0,
            "upload_file_id": "",
            "upload_file_url": mp3_url,
            "detail_file_id": "",
            "detail_file_url": xml_url,
            "score_detail": score_detail,
            "mark_service": 1,
            "spx_file_url": speex_url,
            "mark_trace_id": context.get("mark_trace_id") or '{"engine":""}',
            "use_https": 1,
            "use_homework_privilege": 1,
            "system": config.ETS_CAPTURED_SYSTEM,
            "global_client_version": config.ETS_CAPTURED_APP_VERSION,
            "sn": sn,
            "sign_response": 2,
            "token": token,
            "ets_app": 0,
            "is_zk_pad": 0,
        }
        payload = self.request(ROUTE_AUDIO_SYNC, params, query={"sn": sn})
        body = payload.get("body")
        return body if isinstance(body, dict) else {}

    def submit_choice(
        self,
        token: str,
        context: Dict[str, Any],
        score: str,
        score_detail: str,
        correct: int,
        choose: str,
    ) -> Dict[str, Any]:
        """Submit a multiple-choice answer (no audio involved)."""
        sn = crypto.create_request_sn()
        params = {
            "homework_id": context.get("homework_id", ""),
            "use_time": context.get("use_time", 0),
            "resource_id": context.get("resource_id", config.ETS_EVALUATE_RESOURCE_ID),
            "serial_id": context.get("serial_id", "0"),
            "set_id": context.get("set_id", ""),
            "entity_id": context.get("entity_id", ""),
            "order": context.get("order", ""),
            "score": score,
            "correct": correct,
            "auto_submit": 1,
            "client_time": _client_time(),
            "version": 2,
            "graduation": 0,
            "upload_file_id": "0",
            "upload_file_url": "",
            "detail_file_id": "",
            "detail_file_url": "",
            "score_detail": score_detail,
            "mark_service": 1,
            "spx_file_url": "",
            "mark_trace_id": "",
            "use_https": 1,
            "use_homework_privilege": 1,
            "system": config.ETS_CAPTURED_SYSTEM,
            "global_client_version": config.ETS_CAPTURED_APP_VERSION,
            "sn": sn,
            "sign_response": 2,
            "token": token,
            "ets_app": 0,
            "is_zk_pad": 0,
        }
        payload = self.request(ROUTE_AUDIO_SYNC, params, query={"sn": sn})
        body = payload.get("body")
        return body if isinstance(body, dict) else {}

    def homework_ranking(self, token: str, homework_id: str, size: int = 100) -> List[Dict[str, Any]]:
        """Ranking board for one assignment (plain form POST, not the envelope)."""
        url = f"{config.ETS_API_BASE_URL}/point-service/homework/rank/list"
        response = self.session.post(
            url,
            data={
                "homework_id": homework_id,
                "token": token,
                "size": str(size),
            },
            headers={
                "Host": "api.ets100.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if response.status_code >= 400:
            raise EtsApiError(
                f"排行榜请求失败 HTTP {response.status_code}", route="rank/list"
            )
        payload = response.json()
        code = payload.get("code")
        if code not in (None, "", 0, "0"):
            raise EtsApiError(
                str(payload.get("msg") or f"业务错误 code={code}"),
                code,
                "rank/list",
            )
        return list(payload.get("data") or [])


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _client_time() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
