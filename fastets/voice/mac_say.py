"""macOS system voices (``/usr/bin/say``).

A keyless fallback: it needs no account, no API key and no network, so the
tool still produces audio on a Mac when the configured service is down, over
quota or simply misconfigured.  Windows has no equivalent, so the provider
reports that it is unavailable there.
"""

from __future__ import annotations

import platform
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from .base import ProviderError, TTSResult, VoiceProvider

SAY_BINARY = "/usr/bin/say"
DEFAULT_MAC_VOICE = "Samantha"
# ``say`` speaks at roughly 175 words per minute by default.
DEFAULT_WPM = 175

# A short list of voices that read English well.  ``say -v '?'`` lists every
# voice the system has; ``list_voices`` returns that complete list.
CURATED_VOICES: List[tuple] = [
    ("Samantha", "Samantha（美国女声）"),
    ("Alex", "Alex（美国男声）"),
    ("Daniel", "Daniel（英国男声）"),
    ("Karen", "Karen（澳大利亚女声）"),
    ("Moira", "Moira（爱尔兰女声）"),
    ("Tessa", "Tessa（南非女声）"),
    ("Ava", "Ava（美国女声）"),
    ("Evan", "Evan（美国男声）"),
    ("Nicky", "Nicky（美国女声）"),
    ("Tom", "Tom（美国男声）"),
]


class MacSayProvider(VoiceProvider):
    """Reads a text out loud with the voices that ship with macOS."""

    name = "macos"
    supports_clone = False
    supports_voice_list = True

    def __init__(self, voice: str = DEFAULT_MAC_VOICE, logger=None) -> None:
        super().__init__(logger=logger)
        self.voice = voice or DEFAULT_MAC_VOICE

    # -- plumbing -----------------------------------------------------------

    @staticmethod
    def available() -> bool:
        return platform.system() == "Darwin" and Path(SAY_BINARY).exists()

    def _require_macos(self) -> None:
        if platform.system() != "Darwin":
            raise ProviderError("macOS 系统朗读只能在 macOS 上使用（Windows 没有这个能力）")
        if not Path(SAY_BINARY).exists():
            raise ProviderError(f"macOS 系统朗读不可用：找不到 {SAY_BINARY}")

    # -- synthesis ----------------------------------------------------------

    def synthesize(
        self,
        text: str,
        voice_id: str = "",
        speed: float = 1.0,
        pitch: int = 0,
        language: str = "auto",
    ) -> TTSResult:
        self._require_macos()
        if not text.strip():
            raise ProviderError("待合成文本为空")

        voice = voice_id or self.voice
        try:
            factor = float(speed or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        wpm = int(round(DEFAULT_WPM * max(0.5, min(2.0, factor))))
        # ``say`` has no pitch control; the speed is the only knob it offers.

        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "speech.txt"
            audio = Path(folder) / "speech.wav"
            script.write_text(text, encoding="utf-8")
            command = [
                SAY_BINARY,
                "-v",
                voice,
                "-r",
                str(wpm),
                "--data-format=LEI16@22050",
                "-o",
                str(audio),
                "-f",
                str(script),
            ]
            try:
                process = subprocess.run(
                    command, capture_output=True, timeout=180, check=False
                )
            except FileNotFoundError as exc:
                raise ProviderError(f"macOS 系统朗读不可用: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError("macOS 系统朗读超时") from exc
            if process.returncode != 0:
                detail = process.stderr.decode("utf-8", "replace").strip()
                raise ProviderError(
                    f"macOS 系统朗读失败（音色 {voice}）: {detail or '未知错误'}"
                )
            if not audio.exists() or audio.stat().st_size == 0:
                raise ProviderError(f"macOS 系统朗读没有产生音频（音色 {voice}）")
            payload = audio.read_bytes()

        return TTSResult(
            audio=payload,
            voice_id=voice,
            provider=self.name,
            audio_format="wav",
        )

    # -- voices --------------------------------------------------------------

    def list_voices(self) -> List[Dict[str, Any]]:
        if not self.available():
            return []
        try:
            process = subprocess.run(
                [SAY_BINARY, "-v", "?"], capture_output=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return []
        voices: List[Dict[str, Any]] = []
        for line in process.stdout.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            # "Samantha           en_US    # Hello! My name is Samantha."
            match = re.match(r"^(?P<name>.+?)\s{2,}(?P<locale>[a-zA-Z_]+)\s*#", line)
            if not match:
                continue
            name = match.group("name").strip()
            locale = match.group("locale")
            if not name or not locale:
                continue
            voices.append(
                {
                    "voice_id": name,
                    "name": f"{name}（{locale}）",
                    "group": "macos",
                }
            )
        return voices

    def health_check(self) -> Dict[str, Any]:
        if platform.system() != "Darwin":
            return {
                "provider": self.name,
                "ok": False,
                "message": "macOS 系统朗读只能在 macOS 上使用",
            }
        if not Path(SAY_BINARY).exists():
            return {
                "provider": self.name,
                "ok": False,
                "message": f"找不到 {SAY_BINARY}",
            }
        return {
            "provider": self.name,
            "ok": True,
            "message": f"macOS 系统朗读可用（默认音色 {self.voice}）",
        }
