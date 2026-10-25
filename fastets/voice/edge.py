"""Microsoft Edge TTS provider - free fallback, no API key, no cloning."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from .base import ProviderError, TTSResult, VoiceProvider


class EdgeTTSProvider(VoiceProvider):
    name = "edge"
    supports_clone = False
    supports_voice_list = False

    def __init__(self, voice: str = "en-US-AriaNeural", logger=None) -> None:
        super().__init__(logger=logger)
        self.voice = voice or "en-US-AriaNeural"

    def synthesize(
        self,
        text: str,
        voice_id: str = "",
        speed: float = 1.0,
        pitch: int = 0,
        language: str = "auto",
    ) -> TTSResult:
        if not text.strip():
            raise ProviderError("待合成文本为空")
        try:
            import edge_tts
        except ImportError as exc:
            raise ProviderError(
                "未安装 edge-tts，请执行: pip install edge-tts"
            ) from exc

        chosen = voice_id or self.voice
        rate = f"{int(round((float(speed) - 1.0) * 100)):+d}%"
        pitch_hz = int(pitch)

        async def run() -> bytes:
            communicate = edge_tts.Communicate(text, chosen, rate=rate, pitch=f"{pitch_hz:+d}Hz")
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        try:
            audio = asyncio.run(run())
        except Exception as exc:  # pragma: no cover - network dependent
            raise ProviderError(f"Edge TTS 合成失败: {exc}") from exc
        if not audio:
            raise ProviderError("Edge TTS 返回了空音频")
        return TTSResult(
            audio=audio, voice_id=chosen, provider=self.name, audio_format="mp3"
        )

    def list_voices(self) -> List[Dict[str, Any]]:
        try:
            import asyncio

            import edge_tts
        except ImportError:
            return []

        async def run() -> List[Dict[str, Any]]:
            voices = await edge_tts.list_voices()
            return [
                {"voice_id": v["ShortName"], "name": v["ShortName"], "group": "edge"}
                for v in voices
            ]

        try:
            return asyncio.run(run())
        except Exception:
            return []

    def health_check(self) -> Dict[str, Any]:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return {
                "provider": self.name,
                "ok": False,
                "message": "未安装 edge-tts，请执行: pip install edge-tts",
            }
        return {"provider": self.name, "ok": True, "message": "Edge TTS 可用（无克隆能力）"}
