"""MiniMax 声音克隆 + T2A provider.

Workflow (as documented by MiniMax):

1. ``POST /v1/files/upload`` with ``purpose=voice_clone`` -> ``file_id``
2. optionally a second upload with ``purpose=prompt_audio`` for the second
   (English) sample
3. ``POST /v1/voice_clone`` with both ids -> ``voice_id``
4. ``POST /v1/t2a_v2`` with that ``voice_id`` -> audio bytes
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .base import CloneResult, ProviderError, TTSResult, VoiceProvider

DEFAULT_BASE_URL = "https://api.minimaxi.com"
INTERNATIONAL_BASE_URL = "https://api.minimax.io"


class MiniMaxProvider(VoiceProvider):
    name = "minimax"
    supports_clone = True
    supports_voice_list = True

    def __init__(
        self,
        api_key: str,
        model: str = "speech-2.6-hd",
        language_boost: str = "auto",
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 120,
        logger=None,
    ) -> None:
        super().__init__(logger=logger)
        self.api_key = api_key or ""
        self.model = model or "speech-2.6-hd"
        self.language_boost = language_boost or "auto"
        self.base_url = base_url.rstrip("/") or DEFAULT_BASE_URL
        self.timeout = timeout

    # -- plumbing -----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise ProviderError("未配置 MiniMax API Key，请到「设置」里填写")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = requests.post(
                url,
                headers={**self._headers(), "Content-Type": "application/json"},
                data=json.dumps(payload).encode("utf-8"),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"MiniMax 请求失败 {path}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"MiniMax {path} HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(f"MiniMax {path} 返回了非 JSON 内容") from exc
        status = (body.get("base_resp") or {}).get("status_code")
        if status not in (None, 0, "0"):
            message = str(
                (body.get("base_resp") or {}).get("status_msg") or "MiniMax 调用失败"
            )
            trace = body.get("trace_id", "")
            raise ProviderError(
                f"MiniMax {path} 失败：{message}"
                + (f"（trace_id={trace}）" if trace else "")
            )
        return body

    # -- clone ---------------------------------------------------------------

    def _upload_file(self, path: Path, purpose: str) -> str:
        url = f"{self.base_url}/v1/files/upload"
        try:
            with open(path, "rb") as handle:
                response = requests.post(
                    url,
                    headers=self._headers(),
                    files={"file": (path.name, handle, "application/octet-stream")},
                    data={"purpose": purpose},
                    timeout=self.timeout,
                )
        except requests.RequestException as exc:
            raise ProviderError(f"MiniMax 文件上传失败: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"MiniMax 文件上传 HTTP {response.status_code}: {response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("MiniMax 文件上传返回了非 JSON 内容") from exc
        file_id = body.get("file", {}).get("file_id")
        if file_id in (None, ""):
            raise ProviderError(
                f"MiniMax 文件上传未返回 file_id: {json.dumps(body)[:300]}"
            )
        return str(file_id)

    def clone(
        self,
        sample_path: Path,
        voice_name: str,
        prompt_path: Optional[Path] = None,
        prompt_text: str = "",
    ) -> CloneResult:
        sample_path = Path(sample_path)
        if not sample_path.exists():
            raise ProviderError(f"样本文件不存在: {sample_path}")
        if sample_path.stat().st_size < 1024:
            raise ProviderError(
                f"样本文件太小（{sample_path.stat().st_size} 字节），请重新录制"
            )

        self._info("MiniMax: uploading clone source %s", sample_path.name)
        file_id = self._upload_file(sample_path, "voice_clone")

        payload: Dict[str, Any] = {
            "file_id": int(file_id) if str(file_id).isdigit() else file_id,
            "voice_id": self._safe_name(voice_name, "FastETSVoice"),
            "model": self.model,
            "need_noise_reduction": True,
            "need_volume_normalization": True,
        }

        if prompt_path is not None and Path(prompt_path).exists():
            self._info("MiniMax: uploading prompt audio %s", Path(prompt_path).name)
            prompt_id = self._upload_file(Path(prompt_path), "prompt_audio")
            clone_prompt: Dict[str, Any] = {
                "prompt_audio": (
                    int(prompt_id) if str(prompt_id).isdigit() else prompt_id
                )
            }
            if prompt_text:
                clone_prompt["prompt_text"] = prompt_text
            payload["clone_prompt"] = clone_prompt

        body = self._post_json("/v1/voice_clone", payload)
        voice_id = str(body.get("voice_id") or payload["voice_id"])
        detail = {
            "demo_audio": body.get("demo_audio", ""),
            "extra_info": body.get("extra_info", {}),
        }
        self._info("MiniMax: cloned voice %s", voice_id)
        return CloneResult(voice_id=voice_id, provider=self.name, detail=detail)

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
        if not voice_id:
            raise ProviderError("尚未克隆声音，请先在「声音」页完成预配置")

        voice_setting: Dict[str, Any] = {
            "voice_id": voice_id,
            "speed": float(speed),
            "vol": 1.0,
            "pitch": int(pitch),
        }
        if self.language_boost and self.language_boost != "auto":
            voice_setting["language_boost"] = self.language_boost
        elif language and language != "auto":
            voice_setting["language_boost"] = language

        payload = {
            "model": self.model,
            "text": text,
            "output_format": "hex",
            "voice_setting": voice_setting,
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        body = self._post_json("/v1/t2a_v2", payload)
        data = body.get("data") or {}
        audio = data.get("audio")
        if not audio:
            raise ProviderError(
                "MiniMax 未返回音频数据"
                + self._diagnostics(body, data)
            )

        if isinstance(audio, str) and audio.startswith(("http://", "https://")):
            audio_bytes = self._download(audio)
        else:
            try:
                audio_bytes = bytes.fromhex(str(audio))
            except ValueError as exc:
                raise ProviderError("MiniMax 返回的音频数据无法解析") from exc
        if not audio_bytes:
            raise ProviderError("MiniMax 返回了空音频")

        extra = data.get("extra_info") or {}
        duration = extra.get("audio_length")
        return TTSResult(
            audio=audio_bytes,
            voice_id=voice_id,
            provider=self.name,
            audio_format="mp3",
            duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
            detail={"trace_id": body.get("trace_id", "")},
        )

    def _download(self, url: str) -> bytes:
        try:
            response = requests.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderError(f"下载 MiniMax 音频失败: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"下载 MiniMax 音频 HTTP {response.status_code}")
        return response.content

    @staticmethod
    def _diagnostics(body: Dict[str, Any], data: Dict[str, Any]) -> str:
        """Extra context for an empty MiniMax answer."""
        parts: List[str] = []
        trace = body.get("trace_id")
        if trace:
            parts.append(f"trace_id={trace}")
        status = data.get("status")
        if status is not None:
            parts.append(f"status={status}")
        extra = data.get("extra_info")
        if isinstance(extra, dict) and extra:
            parts.append(
                "extra_info=" + json.dumps(extra, ensure_ascii=False)[:200]
            )
        resp = body.get("base_resp") or {}
        if resp.get("status_msg"):
            parts.append(f"status_msg={resp['status_msg']}")
        if not parts:
            return f"（响应内容：{json.dumps(body, ensure_ascii=False)[:200]}）"
        return "（" + "，".join(parts) + "）"

    # -- voices --------------------------------------------------------------

    def list_voices(self) -> List[Dict[str, Any]]:
        body = self._post_json("/v1/get_voice", {"voice_type": "all"})
        voices: List[Dict[str, Any]] = []
        for group in body.get("system_voice") or []:
            voices.append(
                {
                    "voice_id": str(group.get("voice_id", "")),
                    "name": str(group.get("voice_name", "")),
                    "group": "system",
                }
            )
        for group in body.get("voice_list") or []:
            for voice in group.get("voices") or []:
                voices.append(
                    {
                        "voice_id": str(voice.get("voice_id", "")),
                        "name": str(voice.get("voice_name", "")),
                        "group": "cloned",
                    }
                )
        return voices

    def health_check(self) -> Dict[str, Any]:
        if not self.api_key:
            return {
                "provider": self.name,
                "ok": False,
                "message": "未配置 MiniMax API Key",
            }
        return {"provider": self.name, "ok": True, "message": "MiniMax API Key 已配置"}
