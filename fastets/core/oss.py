"""Aliyun OSS upload for the answer attachments.

The official client asks ``.../ossEvaluateUploadStsToken`` for short lived
credentials and then PUTs three objects (mp3 / speex / xml) directly to OSS.
FastETS reproduces the exact same signature algorithm.
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from . import crypto


class OssError(RuntimeError):
    pass


def _canonical_date() -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())


class OssUploader:
    """Uploads local files to the bucket the ETS100 server hands out."""

    def __init__(self, config_data: Dict[str, Any], timeout: int = 120, logger=None) -> None:
        self.config = config_data or {}
        self.timeout = timeout
        self._log = logger

    # -- helpers -------------------------------------------------------------

    def _info(self, message: str, *args: Any) -> None:
        if self._log:
            self._log.info(message, *args)

    def _validate(self) -> None:
        missing = [
            key
            for key in ("access_key_id", "access_key_secret", "security_token", "bucket")
            if not self.config.get(key)
        ]
        if missing:
            raise OssError(f"OSS 配置不完整，缺少: {', '.join(missing)}")

    def _prefix_for(self, kind: str) -> str:
        if kind == "xml":
            return str(self.config.get("xml_path") or "")
        if kind == "speex":
            return str(self.config.get("spx_path") or "")
        return str(self.config.get("mp3_path") or "")

    def object_key(self, kind: str, extension: str) -> str:
        prefix = self._prefix_for(kind)
        name = f"public_{crypto.random_hex(32).lower()}_{int(time.time() * 1000)}{extension}"
        return f"{prefix}{name}"

    def public_url(self, object_key: str) -> str:
        cname = str(self.config.get("cname") or "").rstrip("/")
        if cname:
            return f"{cname}/{object_key}"
        bucket = str(self.config.get("bucket") or "")
        endpoint = str(self.config.get("endpoint") or "")
        return f"https://{bucket}.{endpoint}/{object_key}"

    # -- upload --------------------------------------------------------------

    def upload(self, local_path: Path, kind: str) -> Dict[str, str]:
        """Upload ``local_path`` and return ``{"object_key", "url"}``."""
        self._validate()
        local_path = Path(local_path)
        if not local_path.exists():
            raise OssError(f"待上传文件不存在: {local_path}")

        extension = local_path.suffix or (
            ".xml" if kind == "xml" else ".speex" if kind == "speex" else ".mp3"
        )
        object_key = self.object_key(kind, extension)
        payload = local_path.read_bytes()

        bucket = str(self.config["bucket"])
        endpoint = str(self.config["endpoint"])
        secret = str(self.config["access_key_secret"])
        access_id = str(self.config["access_key_id"])
        security_token = str(self.config["security_token"])

        content_type = "application/octet-stream"
        content_md5 = base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")
        date = _canonical_date()
        canonical_resource = f"/{bucket}/{object_key}"
        canonical_headers = (
            f"x-oss-object-acl:public-read\n" f"x-oss-security-token:{security_token}\n"
        )
        string_to_sign = (
            f"PUT\n{content_md5}\n{content_type}\n{date}\n"
            f"{canonical_headers}{canonical_resource}"
        )
        signature = base64.b64encode(
            hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")

        url = f"https://{bucket}.{endpoint}/{object_key}"
        self._info("OSS upload %s -> %s", kind, url)
        try:
            response = requests.put(
                url,
                data=payload,
                headers={
                    "Authorization": f"OSS {access_id}:{signature}",
                    "x-oss-object-acl": "public-read",
                    "x-oss-security-token": security_token,
                    "Date": date,
                    "Content-MD5": content_md5,
                    "Content-Type": content_type,
                    "Content-Length": str(len(payload)),
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise OssError(f"OSS 上传失败: {exc}") from exc
        if response.status_code >= 400:
            raise OssError(f"OSS 上传 HTTP {response.status_code}: {response.text[:300]}")
        return {"object_key": object_key, "url": self.public_url(object_key)}
