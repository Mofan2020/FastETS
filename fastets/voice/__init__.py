"""Voice cloning and text-to-speech providers.

FastETS ships three interchangeable providers:

``minimax``        MiniMax 声音克隆 + T2A. The only provider that can clone a
                   voice from a recording (Chinese + English sample).
``openai_compat``  Any OpenAI compatible ``/audio/speech`` endpoint. Works with
                   OpenAI itself, Azure, Groq, and local clone servers such as
                   GPT-SoVITS / CosyVoice / IndexTTS that expose a voice name.
``edge``           Microsoft Edge TTS. No API key, no cloning - a free fallback
                   so the tool still works out of the box.
``macos``          The voices that ship with macOS (``say``). No account, no
                   network - the last resort on a Mac.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import CloneResult, ProviderError, TTSResult, VoiceProvider
from .edge import EdgeTTSProvider
from .mac_say import DEFAULT_MAC_VOICE, MacSayProvider
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
    "MacSayProvider",
    "build_provider",
    "build_fallback_provider",
    "resolve_voice_id",
    "DEFAULT_MINIMAX_VOICE",
    "DEFAULT_EDGE_VOICE",
    "DEFAULT_MAC_VOICE",
    "PROVIDER_NAMES",
]

PROVIDER_NAMES = {
    "minimax": "MiniMax 声音克隆 + T2A",
    "openai_compat": "OpenAI 兼容接口",
    "edge": "Microsoft Edge TTS（免密钥）",
    "macos": "macOS 系统朗读（免密钥，仅 macOS）",
}

# A built in MiniMax system voice, used when the user does not want to clone
# a voice of their own.
DEFAULT_MINIMAX_VOICE = "male-qn-qingse"
DEFAULT_EDGE_VOICE = "en-US-AriaNeural"
DEFAULT_OPENAI_VOICE = "alloy"


def resolve_voice_id(settings: dict, provider: VoiceProvider) -> str:
    """The voice identifier that the active provider should speak with.

    ``voice_mode`` decides whether the cloned voice is used or a plain
    system voice.  When the user did not clone anything yet the default
    voice is used as well, so that a run is never blocked by a missing
    clone.
    """
    settings = settings or {}
    mode = str(settings.get("voice_mode") or "clone").strip().lower()
    chosen = str(settings.get("default_voice") or "").strip()

    if mode == "default" and chosen:
        return chosen

    if provider.name == "openai_compat":
        voice = str(settings.get("openai_voice") or "")
    elif provider.name == "edge":
        voice = str(settings.get("edge_voice") or "")
    elif provider.name == "macos":
        voice = str(settings.get("macos_voice") or "")
    else:
        voice = str(settings.get("minimax_voice_id") or "")
    if voice:
        return voice

    if chosen:
        return chosen
    if provider.name == "openai_compat":
        return DEFAULT_OPENAI_VOICE
    if provider.name == "edge":
        return DEFAULT_EDGE_VOICE
    if provider.name == "macos":
        return DEFAULT_MAC_VOICE
    return DEFAULT_MINIMAX_VOICE


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
    if name in ("macos", "mac", "say", "macos_say"):
        return MacSayProvider(
            voice=str(settings.get("macos_voice") or DEFAULT_MAC_VOICE), logger=logger
        )
    raise ProviderError(f"未知的语音服务: {name}")


def build_fallback_provider(settings: dict, logger=None) -> Optional[VoiceProvider]:
    """The provider that takes over when the configured one fails.

    ``tts_fallback`` picks it: ``macos`` uses the voices that ship with
    macOS, ``edge`` uses Microsoft Edge TTS and ``none`` disables the
    fallback.  It returns ``None`` when the fallback is not usable here.
    """
    settings = settings or {}
    name = str(settings.get("tts_fallback") or "").strip().lower()
    if name in ("", "none", "off", "关闭"):
        return None
    if name in ("macos", "mac", "say", "macos_say"):
        if not MacSayProvider.available():
            return None
        return MacSayProvider(
            voice=str(settings.get("macos_voice") or DEFAULT_MAC_VOICE), logger=logger
        )
    if name == "edge":
        return EdgeTTSProvider(
            voice=str(settings.get("edge_voice") or "en-US-AriaNeural"), logger=logger
        )
    return None
