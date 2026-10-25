"""Provider interface shared by every FastETS speech backend."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProviderError(RuntimeError):
    """Raised when the speech backend fails or is not configured."""


@dataclass
class CloneResult:
    """Outcome of a voice-clone request."""

    voice_id: str
    provider: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TTSResult:
    """A synthesised audio clip."""

    audio: bytes
    voice_id: str
    provider: str
    audio_format: str = "mp3"
    duration_ms: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)


class VoiceProvider:
    """Base class: synthesize speech, optionally clone a voice first."""

    name = "base"
    supports_clone = False
    supports_voice_list = False

    def __init__(self, logger=None) -> None:
        self._log = logger

    # -- logging -------------------------------------------------------------

    def _info(self, message: str, *args: Any) -> None:
        if self._log:
            self._log.info(message, *args)

    def _debug(self, message: str, *args: Any) -> None:
        if self._log:
            self._log.debug(message, *args)

    def _error(self, message: str, *args: Any) -> None:
        if self._log:
            self._log.error(message, *args)

    # -- API -----------------------------------------------------------------

    def synthesize(
        self,
        text: str,
        voice_id: str = "",
        speed: float = 1.0,
        pitch: int = 0,
        language: str = "auto",
    ) -> TTSResult:
        raise NotImplementedError

    def clone(
        self,
        sample_path: Path,
        voice_name: str,
        prompt_path: Optional[Path] = None,
        prompt_text: str = "",
    ) -> CloneResult:
        raise NotImplementedError(
            f"语音服务 {self.name} 不支持声音克隆，请改用 MiniMax 或配置克隆端点"
        )

    def list_voices(self) -> List[Dict[str, Any]]:
        return []

    def health_check(self) -> Dict[str, Any]:
        """Return a dict describing whether the provider is usable right now."""
        return {"provider": self.name, "ok": True, "message": ""}

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    @staticmethod
    def _safe_name(value: str, fallback: str = "fastets") -> str:
        cleaned = "".join(
            char if char.isalnum() or char in "-_" else "_" for char in value
        ).strip("_")
        return cleaned or fallback
