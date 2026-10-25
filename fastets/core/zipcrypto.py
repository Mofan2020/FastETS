"""ETS100 resource packs: password derivation and extraction.

Every assignment resource is a ZIP whose tail carries a 336 byte footer. The
unlock password is derived from a 128 byte seed inside that footer:

    seed        = footer[16:144]
    password    = MD5(seed).upper() + MD5(MD5(seed).upper()).upper()
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from .. import config

FOOTER_SIZE = config.ETS_ZIP_FOOTER_SIZE
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class ResourcePackError(RuntimeError):
    pass


def check_zip_magic(data: bytes) -> None:
    if len(data) < 4 or data[:4] not in ZIP_MAGIC:
        raise ResourcePackError(
            "下载到的资源不是 ZIP 文件（可能返回了错误页面），请稍后重试"
        )


def generate_zip_password(data: bytes) -> str:
    """Derive the unlock password of an ETS100 resource pack."""
    import hashlib

    if len(data) < FOOTER_SIZE:
        raise ResourcePackError("资源包体积过小，无法读取尾部校验数据")
    footer = data[-FOOTER_SIZE:]
    if not (footer[0:8] == b"MSTCHINA" or footer[144:149] == b"EPLAT"):
        raise ResourcePackError("资源包签名校验失败，可能不是 ETS100 资源包")
    seed = footer[16:144]
    first_hex = hashlib.md5(seed).hexdigest().upper()
    second_hex = hashlib.md5(first_hex.encode("ascii")).hexdigest().upper()
    return first_hex + second_hex


def _extract_with_pyzipper(data: bytes, password: str, target: Path) -> Path:
    try:
        import pyzipper
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise ResourcePackError(
            "缺少 pyzipper 依赖，请执行: pip install pyzipper"
        ) from exc

    with pyzipper.AESZipFile(io.BytesIO(data)) as archive:
        archive.setpassword(password.encode("utf-8"))
        names = archive.namelist()
        if not names:
            raise ResourcePackError("资源包为空")
        archive.extractall(str(target), pwd=password.encode("utf-8"))
    return target


def _extract_with_zipfile(data: bytes, password: str, target: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        if not names:
            raise ResourcePackError("资源包为空")
        for name in names:
            if name.endswith("/"):
                continue
            # pyzipper's AES handling is not available in the stdlib; this path
            # only works for classic ZipCrypto archives.
            try:
                archive.extract(name, str(target), pwd=password.encode("utf-8"))
            except RuntimeError as exc:
                raise ResourcePackError(
                    "资源包使用 AES 加密，请安装 pyzipper: pip install pyzipper"
                ) from exc
    return target


def extract_resource_pack(data: bytes, target: Path) -> Path:
    """Extract an ETS100 resource pack and return the directory."""
    check_zip_magic(data)
    password = generate_zip_password(data)
    target.mkdir(parents=True, exist_ok=True)
    try:
        return _extract_with_pyzipper(data, password, target)
    except ResourcePackError:
        raise
    except Exception:
        return _extract_with_zipfile(data, password, target)


def find_content_json(root: Path) -> Optional[Path]:
    """Locate ``content.json`` inside an extracted resource pack."""
    direct = root / "content.json"
    if direct.exists():
        return direct
    for candidate in root.rglob("content.json"):
        return candidate
    return None


def cache_path_for(url: str, suffix: str = ".zip") -> Path:
    """Stable cache file name for a resource URL."""
    import hashlib

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return config.papers_cache_dir() / f"{digest}{suffix}"


def read_content_json(root: Path) -> Dict[str, Any]:
    import json

    path = find_content_json(root)
    if path is None:
        raise ResourcePackError("资源包中缺少 content.json")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
