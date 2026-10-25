"""Audio helpers: ffmpeg conversion and Ogg/Speex unwrapping.

The ETS100 server expects three attachments per answer:

``mp3``    the recording (16 kHz mono)
``speex``  the raw Speex payload of the same recording
``xml``    the evaluation detail document

FFmpeg is required. FastETS prefers the static binary shipped by
``imageio-ffmpeg`` so that users do not have to install anything, and falls
back to a system installation or a user configured path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config


class AudioError(RuntimeError):
    pass


class SpeexUnavailableError(AudioError):
    """Raised when no Speex encoder can be found on this machine."""


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_STREAM_RE = re.compile(r"Stream #\d+:\d+.*?Audio:\s*([A-Za-z0-9_]+)")
_SAMPLE_RATE_RE = re.compile(r"(\d+)\s*Hz")
_CHANNELS_RE = re.compile(r"(mono|stereo|quad|\d+\.\d+ channels)")
_MEAN_VOLUME_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")


def _bundled_ffmpeg() -> Optional[str]:
    """Locate the ffmpeg binary shipped inside a PyInstaller bundle."""
    import sys
    from pathlib import Path

    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parents[2])
    for root in roots:
        for folder in ("fastets/bin", "bin", "."):
            candidate_dir = root / folder
            if not candidate_dir.is_dir():
                continue
            for pattern in ("ffmpeg*", "ffmpeg.exe"):
                for candidate in candidate_dir.glob(pattern):
                    if candidate.is_file():
                        return str(candidate)
    return None


def resolve_ffmpeg(configured: str = "") -> str:
    """Return a usable ffmpeg executable path."""
    if configured and (os.path.isabs(configured) or Path(configured).exists()):
        return configured
    if configured and shutil.which(configured):
        return configured
    env = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if env and Path(env).exists():
        return env
    bundled = _bundled_ffmpeg()
    if bundled:
        return bundled
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise AudioError(
        "找不到 ffmpeg。请安装 ffmpeg 并加入 PATH，或在设置里指定 ffmpeg 路径"
    )


def _run(executable: str, args: List[str], timeout: int = 180) -> None:
    try:
        result = subprocess.run(
            [executable, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise AudioError(f"ffmpeg 不存在: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg 执行超时: {' '.join(args[:4])}") from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")[-500:]
        raise AudioError(f"ffmpeg 执行失败: {stderr}")


def extract_raw_speex_from_ogg(data: bytes) -> bytes:
    """Strip the Ogg container and return the raw Speex frames."""
    packets: List[bytes] = []
    current = bytearray()
    offset = 0
    length = len(data)
    while offset + 27 <= length:
        if data[offset : offset + 4] != b"OggS":
            raise AudioError(f"Ogg Speex 页头无效，offset={offset}")
        segment_count = data[offset + 26]
        segment_table_start = offset + 27
        payload_offset = segment_table_start + segment_count
        if payload_offset > length:
            raise AudioError("Ogg Speex 页表越界")
        for index in range(segment_count):
            segment_length = data[offset + 27 + index]
            end = payload_offset + segment_length
            if end > length:
                raise AudioError("Ogg Speex 页载荷越界")
            current += data[payload_offset:end]
            payload_offset = end
            if segment_length < 255:
                packets.append(bytes(current))
                current = bytearray()
        offset = payload_offset
    if not packets:
        raise AudioError("Ogg Speex 中没有音频数据")

    header_index = -1
    for index, packet in enumerate(packets):
        if packet[:8].startswith(b"Speex"):
            header_index = index
            break
    audio_packets = packets[header_index + 2 :] if header_index >= 0 else packets
    raw = b"".join(audio_packets)
    if not raw:
        raise AudioError("Speex 编码结果为空")
    return raw


class AudioTools:
    """Thin wrapper around the ffmpeg binary."""

    def __init__(self, ffmpeg_path: str = "", logger=None) -> None:
        self._configured = ffmpeg_path
        self._resolved: Optional[str] = None
        self._log = logger

    @property
    def ffmpeg(self) -> str:
        if self._resolved is None:
            self._resolved = resolve_ffmpeg(self._configured)
        return self._resolved

    def to_mp3(self, source: Path, target: Optional[Path] = None) -> Path:
        """Convert any audio file to 16 kHz mono mp3."""
        source = Path(source)
        if not source.exists():
            raise AudioError(f"音频文件不存在: {source}")
        target = Path(target or config.audio_cache_dir() / f"{crypto_id()}.mp3")
        _run(
            self.ffmpeg,
            [
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(target),
            ],
        )
        return target

    def to_speex(self, source: Path, target: Optional[Path] = None) -> Path:
        """Convert any audio file to a raw Speex payload.

        ffmpeg only ships the ``libspeex`` encoder up to version 5.x, so the
        conversion falls back to the helper that FastETS bundles (a tiny
        command line front end for libspeex).
        """
        source = Path(source)
        if not source.exists():
            raise AudioError(f"音频文件不存在: {source}")
        target = Path(target or config.audio_cache_dir() / f"{crypto_id()}.speex")

        if self.has_encoder("libspeex"):
            ogg_path = target.with_suffix(".ogg.tmp.spx")
            _run(
                self.ffmpeg,
                [
                    "-y",
                    "-i",
                    str(source),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-codec:a",
                    "libspeex",
                    "-f",
                    "spx",
                    str(ogg_path),
                ],
            )
            try:
                raw = extract_raw_speex_from_ogg(ogg_path.read_bytes())
            finally:
                if ogg_path.exists():
                    ogg_path.unlink()
            target.write_bytes(raw)
            return target

        from . import speex as speex_mod

        return speex_mod.encode_with_helper(self.ffmpeg, source, target)

    def speex_status(self) -> Dict[str, Any]:
        """Describe which Speex encoder this machine can use."""
        from . import speex as speex_mod

        if self.has_encoder("libspeex"):
            return {
                "available": True,
                "method": "ffmpeg",
                "message": "ffmpeg 自带 libspeex 编码器",
            }
        if speex_mod.helper_available():
            return {
                "available": True,
                "method": "helper",
                "message": f"使用内置 Speex 编码器 {speex_mod.find_helper()}",
            }
        return {
            "available": False,
            "method": "none",
            "message": (
                "本机没有可用的 Speex 编码器，提交时会省略 spx 附件"
                "（mp3 与评测 XML 仍会正常上传，分数不受影响）"
            ),
        }

    def duration_ms(self, source: Path) -> int:
        """Duration of an audio file in milliseconds.

        The bundled binary is a plain ``ffmpeg`` without a sibling
        ``ffprobe``, so the duration is read from ffmpeg's own output.  The old
        ffprobe based probe always failed and silently reported 0 - which is
        why every submission used to go out with ``use_time=0``.
        """
        return int(self.probe(source).get("duration_ms") or 0)

    # -- diagnostics ---------------------------------------------------------

    def probe(self, source: Path) -> Dict[str, Any]:
        """Describe an audio file: container, codec, rate, channels, length.

        Uses ``ffmpeg -i`` instead of ``ffprobe`` because the bundled binary is
        a plain ``ffmpeg`` without a sibling ``ffprobe``.
        """
        source = Path(source)
        info: Dict[str, Any] = {
            "path": str(source),
            "exists": source.exists(),
            "size": source.stat().st_size if source.exists() else 0,
            "duration_ms": 0,
            "codec": "",
            "sample_rate": 0,
            "channels": "",
            "format": "",
        }
        if not source.exists():
            return info
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-i", str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return info
        text = result.stderr.decode("utf-8", "replace")
        match = _DURATION_RE.search(text)
        if match:
            hours, minutes, seconds = match.groups()
            info["duration_ms"] = int(
                (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
            )
        codec = _STREAM_RE.search(text)
        if codec:
            info["codec"] = codec.group(1)
        rate = _SAMPLE_RATE_RE.search(text)
        if rate:
            info["sample_rate"] = int(rate.group(1))
        channels = _CHANNELS_RE.search(text)
        if channels:
            info["channels"] = channels.group(1)
        fmt = re.search(r"Input #0,\s*([^,\n]+),", text)
        if fmt:
            info["format"] = fmt.group(1).strip()
        return info

    def mean_volume_db(self, source: Path) -> Optional[float]:
        """Average loudness of a file in dB (``None`` when it cannot be measured)."""
        source = Path(source)
        if not source.exists():
            return None
        try:
            result = subprocess.run(
                [
                    self.ffmpeg,
                    "-hide_banner",
                    "-nostats",
                    "-i",
                    str(source),
                    "-af",
                    "volumedetect",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        text = result.stderr.decode("utf-8", "replace")
        match = _MEAN_VOLUME_RE.search(text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
        return None

    def is_silent(self, source: Path, threshold_db: float = -45.0) -> bool:
        """True when the file carries no usable signal (pure silence)."""
        volume = self.mean_volume_db(source)
        if volume is None:
            return False
        return volume <= threshold_db

    def has_encoder(self, name: str) -> bool:
        """Whether the resolved ffmpeg exposes the given encoder."""
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        text = result.stdout.decode("utf-8", "replace")
        pattern = re.compile(rf"^\s*\S+\s+{re.escape(name)}\b", re.MULTILINE)
        return bool(pattern.search(text))

    def validate(self) -> None:
        _run(self.ffmpeg, ["-version"], timeout=30)


def crypto_id() -> str:
    import uuid

    return uuid.uuid4().hex
