"""Speex encoding for the ETS100 ``spx`` attachment.

Every answer that the ETS100 cloud stores carries the recording twice: as an
mp3 (``upload_file_url``, used for playback) and as a raw Speex payload
(``spx_file_url``).  The raw payload is the concatenation of the Speex frames
without any container - exactly what ffmpeg's Ogg Speex muxer would have put
into the Ogg packets.

ffmpeg shipped the ``libspeex`` encoder until version 5.x; it was removed in
ffmpeg 6.0, so every current ffmpeg (including the static binary FastETS
bundles) cannot produce Speex any more.  FastETS therefore ships a tiny helper
(``speexenc_raw``) that is statically linked against libspeex, see
``packaging/native/``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from .audio import AudioError, SpeexUnavailableError

HELPER_NAMES = ("speexenc_raw", "speexenc_raw.exe")

DEFAULT_RATE = 16000
DEFAULT_QUALITY = 8


def _helper_roots() -> List[Path]:
    roots: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(__file__).resolve().parents[2])
    return roots


def find_helper() -> Optional[str]:
    """Locate the bundled Speex helper binary."""
    for root in _helper_roots():
        for folder in ("fastets/bin", "bin", "."):
            candidate_dir = root / folder
            if not candidate_dir.is_dir():
                continue
            for name in HELPER_NAMES:
                candidate = candidate_dir / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    return None


def helper_available() -> bool:
    return find_helper() is not None


def encode_with_helper(
    ffmpeg: str,
    source: Path,
    target: Path,
    rate: int = DEFAULT_RATE,
    quality: int = DEFAULT_QUALITY,
) -> Path:
    """Encode ``source`` into the raw Speex payload the ETS100 server expects.

    ffmpeg decodes the input into mono 16 bit PCM (the same sample format
    ffmpeg's own libspeex encoder uses), the helper turns that PCM into Speex
    frames.  The result is byte identical to what ffmpeg's ``libspeex``
    encoder produces (same libspeex source, same quality / complexity /
    CBR settings, tail frame zero padded like ffmpeg's audio FIFO does).

    The bit for bit comparison was measured on macOS arm64.  On x86_64 the
    fused multiply-add instructions make a few quantiser decisions differ, so
    the frames are equivalent but not identical there; the stream stays a
    valid Speex stream of the same quality.
    """
    helper = find_helper()
    if not helper:
        raise SpeexUnavailableError(
            "内置的 Speex 编码器缺失（speexenc_raw），本次提交将不附带 spx 文件"
        )

    source = Path(source)
    target = Path(target)
    if not source.exists():
        raise AudioError(f"音频文件不存在: {source}")

    handle = tempfile.NamedTemporaryFile(suffix=".s16le", delete=False)
    pcm_path = Path(handle.name)
    handle.close()
    try:
        decode = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "quiet",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(rate),
                "-f",
                "s16le",
                str(pcm_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
        if decode.returncode != 0:
            raise AudioError(
                "ffmpeg 解码失败: "
                + decode.stderr.decode("utf-8", "replace")[-300:]
            )
        if not pcm_path.exists() or pcm_path.stat().st_size == 0:
            raise AudioError("ffmpeg 没有解出任何音频数据")

        with pcm_path.open("rb") as pcm:
            encode = subprocess.run(
                [
                    helper,
                    "--rate",
                    str(rate),
                    "--quality",
                    str(quality),
                ],
                stdin=pcm,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
            )
        if encode.returncode != 0:
            raise AudioError(
                "Speex 编码失败: " + encode.stderr.decode("utf-8", "replace")[-300:]
            )
        payload = encode.stdout
        if not payload:
            raise AudioError("Speex 编码结果为空")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target
    finally:
        try:
            if pcm_path.exists():
                pcm_path.unlink()
        except OSError:
            pass
