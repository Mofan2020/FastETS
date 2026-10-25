"""OpenAI compatible text-to-speech provider.

Targets any service that exposes ``POST {base_url}/audio/speech`` with the
OpenAI body shape::

    {"model": "...", "voice": "...", "input": "...", "response_format": "mp3"}

That includes OpenAI itself, Azure OpenAI, Groq, Together, and the many local
voice-clone servers (GPT-SoVITS, CosyVoice, IndexTTS, F5-TTS ...) that expose
a cloned voice through the same endpoint under a custom voice name.

Because such servers normally do their own cloning, an optional clone endpoint
can be configured with :setting:`openai_clone_endpoint`. FastETS posts the
sample (and optional prompt sample) as multipart form data and reads the voice
id back from the JSON response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .base import CloneResult, ProviderError, TTSResult, VoiceProvider

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatProvider(VoiceProvider):
    name = "openai_compat"
    supports_clone = True  # only when a clone endpoint is configured
    supports_voice_list = True

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "tts-1",
        voice: str = "alloy",
        clone_endpoint: str = "",
        timeout: int = 120,
        logger=None,
    ) -> None:
        super().__init__(logger=logger)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or ""
        self.model = model or "tts-1"
        self.voice = voice or "alloy"
        self.clone_endpoint = clone_endpoint or ""
        self.timeout = timeout

    # -- plumbing -----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _require_base(self) -> None:
        if not self.base_url:
            raise ProviderError("未配置 OpenAI 兼容端点地址 (Base URL)")

    # -- synthesis -----------------------------------------------------------

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
        self._require_base()

        payload: Dict[str, Any] = {
            "model": self.model,
            "voice": voice_id or self.voice,
            "input": text,
            "response_format": "mp3",
        }
        if speed and float(speed) != 1.0:
            payload["speed"] = float(speed)

        url = f"{self.base_url}/audio/speech"
        self._info("OpenAI-compatible TTS -> %s", url)
        try:
            response = requests.post(
                url,
                headers=self._headers(),
                data=json.dumps(payload).encode("utf-8"),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"OpenAI 兼容端点请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"OpenAI 兼容端点 HTTP {response.status_code}: {response.text[:300]}"
            )

        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            # Some servers answer with {"audio": "<url>"} or {"data": {"url": ...}}
            body = response.json()
            url = _extract_audio_url(body)
            if not url:
                raise ProviderError(
                    f"OpenAI 兼容端点返回了 JSON 但没有音频: {json.dumps(body)[:200]}"
                )
            audio = self._download(url)
        else:
            audio = response.content

        if not audio:
            raise ProviderError("OpenAI 兼容端点返回了空音频")
        return TTSResult(
            audio=audio,
            voice_id=voice_id or self.voice,
            provider=self.name,
            audio_format="mp3",
        )

    def _download(self, url: str) -> bytes:
        try:
            response = requests.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"下载音频失败: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"下载音频 HTTP {response.status_code}")
        return response.content

    # -- clone ---------------------------------------------------------------

    def clone(
        self,
        sample_path: Path,
        voice_name: str,
        prompt_path: Optional[Path] = None,
        prompt_text: str = "",
    ) -> CloneResult:
        if not self.clone_endpoint:
            raise ProviderError(
                "该 OpenAI 兼容端点未配置克隆接口。"
                "请在设置里填写「克隆端点」，或改用 MiniMax 声音克隆。"
            )
        sample_path = Path(sample_path)
        if not sample_path.exists():
            raise ProviderError(f"样本文件不存在: {sample_path}")

        files = {"file": (sample_path.name, open(sample_path, "rb"), "application/octet-stream")}
        data = {"voice": voice_name, "voice_name": voice_name}
        if prompt_path is not None and Path(prompt_path).exists():
            files["prompt_file"] = (
                Path(prompt_path).name,
                open(prompt_path, "rb"),
                "application/octet-stream",
            )
            if prompt_text:
                data["prompt_text"] = prompt_text
        try:
            response = requests.post(
                self.clone_endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                files=files,
                data=data,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"克隆端点请求失败: {exc}") from exc
        finally:
            for handle in files.values():
                try:
                    handle[1].close()
                except Exception:
                    pass
        if response.status_code >= 400:
            raise ProviderError(
                f"克隆端点 HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("克隆端点返回了非 JSON 内容") from exc

        voice_id = _extract_voice_id(body) or voice_name
        return CloneResult(voice_id=voice_id, provider=self.name, detail={"raw": body})

    # -- voices --------------------------------------------------------------

    def list_voices(self) -> List[Dict[str, Any]]:
        if not self.base_url:
            return []
        try:
            response = requests.get(
                f"{self.base_url}/voices",
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException:
            return []
        if response.status_code >= 400:
            return []
        try:
            body = response.json()
        except ValueError:
            return []
        voices: List[Dict[str, Any]] = []
        for item in body.get("data") or []:
            if isinstance(item, dict):
                voices.append(
                    {
                        "voice_id": str(item.get("id") or item.get("voice") or ""),
                        "name": str(item.get("name") or item.get("id") or ""),
                        "group": "remote",
                    }
                )
        return voices

    def health_check(self) -> Dict[str, Any]:
        if not self.base_url:
            return {"provider": self.name, "ok": False, "message": "未配置 Base URL"}
        if not self.api_key:
            return {
                "provider": self.name,
                "ok": True,
                "message": "未配置 API Key（部分本地服务不需要）",
            }
        return {"provider": self.name, "ok": True, "message": "OpenAI 兼容端点已配置"}


def _extract_audio_url(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("audio", "url", "audio_url", "data"):
            value = body.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
            if isinstance(value, dict):
                nested = _extract_audio_url(value)
                if nested:
                    return nested
    return ""


def _extract_voice_id(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("voice_id", "voice", "voiceId", "id"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
        for key in ("data", "result", "body"):
            value = body.get(key)
            if isinstance(value, (dict, list)):
                nested = _extract_voice_id(value)
                if nested:
                    return nested
    if isinstance(body, list):
        for item in body:
            nested = _extract_voice_id(item)
            if nested:
                return nested
    return ""
