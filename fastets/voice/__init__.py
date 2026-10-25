"""Voice cloning and text-to-speech providers.

FastETS ships three interchangeable providers:

``minimax``        MiniMax 声音克隆 + T2A. The only provider that can clone a
                   voice from a recording (Chinese + English sample).
``openai_compat``  Any OpenAI compatible ``/audio/speech`` endpoint. Works with
                   OpenAI itself, Azure, Groq, and local clone servers such as
                   GPT-SoVITS / CosyVoice / IndexTTS that expose a voice name.
``edge``           Microsoft Edge TTS. No API key, no cloning - a free fallback
                   so the tool still works out of the box.
"""

from __future__ import annotations

from .base import CloneResult, ProviderError, TTSResult, VoiceProvider
from .edge import EdgeTTSProvider
from .minimax import DEFAULT_BASE_URL as DEFAULT_MINIMAX_BASE_URL
from .minimax import MiniMaxProvider
from .openai_compat import OpenAICompatProvider

__all__ = [
    "CloneResult",
    "ProviderError",
    "TTSResult",
    "VoiceProvider",
    "MiniMaxProvider",
    "OpenAICompatProvider",
    "EdgeTTSProvider",
    "build_provider",
    "PROVIDER_NAMES",
]

PROVIDER_NAMES = {
    "minimax": "MiniMax 声音克隆 + T2A",
    "openai_compat": "OpenAI 兼容接口",
    "edge": "Microsoft Edge TTS（免密钥）",
}


def build_provider(settings: dict, logger=None) -> VoiceProvider:
    """Instantiate the provider selected in ``settings``."""
    name = str(settings.get("tts_provider") or "minimax").lower()
    if name == "minimax":
        return MiniMaxProvider(
            api_key=str(settings.get("minimax_api_key") or ""),
            model=str(settings.get("minimax_model") or "speech-2.6-hd"),
            language_boost=str(settings.get("minimax_language_boost") or "auto"),
            base_url=str(
                settings.get("minimax_base_url") or DEFAULT_MINIMAX_BASE_URL
            ),
            logger=logger,
        )
    if name in ("openai", "openai_compat", "openai-compatible"):
        return OpenAICompatProvider(
            base_url=str(settings.get("openai_base_url") or ""),
            api_key=str(settings.get("openai_api_key") or ""),
            model=str(settings.get("openai_model") or "tts-1"),
            voice=str(settings.get("openai_voice") or "alloy"),
            clone_endpoint=str(settings.get("openai_clone_endpoint") or ""),
            logger=logger,
        )
    if name == "edge":
        return EdgeTTSProvider(
            voice=str(settings.get("edge_voice") or "en-US-AriaNeural"), logger=logger
        )
    raise ProviderError(f"未知的语音服务: {name}")
