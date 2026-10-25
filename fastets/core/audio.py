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
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .. import config


class AudioError(RuntimeError):
    pass


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
        """Convert any audio file to a raw Speex payload."""
        source = Path(source)
        if not source.exists():
            raise AudioError(f"音频文件不存在: {source}")
        target = Path(target or config.audio_cache_dir() / f"{crypto_id()}.speex")
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

    def duration_ms(self, source: Path) -> int:
        """Duration of an audio file in milliseconds."""
        try:
            result = subprocess.run(
                [
                    self.ffmpeg.replace("ffmpeg", "ffprobe") if self.ffmpeg.endswith("ffmpeg") else "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            value = result.stdout.decode("utf-8", "replace").strip()
            return int(float(value) * 1000) if value else 0
        except Exception:
            return 0

    def validate(self) -> None:
        _run(self.ffmpeg, ["-version"], timeout=30)


def crypto_id() -> str:
    import uuid

    return uuid.uuid4().hex
