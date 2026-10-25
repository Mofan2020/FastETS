"""Filesystem layout and persisted settings for FastETS."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict

APP_NAME = "FastETS"

# --- ETS100 protocol constants (see docs/API.md) ----------------------------
ETS_API_BASE_URL = "https://api.ets100.com"
ETS_CDN_BASE_URL = "https://cdn.subject.ets100.com"
ETS_PID = "grlx"
ETS_SECRET_KEY = "555ffbe95ccf4e9535a110170b445ab8"
ETS_USER_AGENT = "libcurl-agent/1.0"

# Legacy protocol profile (client 5.4.5) -----------------------------------
ETS_DEFAULT_SN = "test"
ETS_DEFAULT_SYSTEM = "4"
ETS_DEFAULT_VERSION = "3"
ETS_DEFAULT_GLOBAL_CLIENT_VERSION = "5.4.5"
ETS_DEFAULT_DEVICE_NAME = "DESKTOP"
ETS_DEFAULT_REBIND_VERSION = "2"
ETS_DEFAULT_REBIND_DEVICE_NAME = "1337"
ETS_DEFAULT_LOCAL_IP = "127.0.0.1"

# Captured protocol profile (client 7.0.71.2569) ----------------------------
ETS_CAPTURED_APP_VERSION = "7.0.71.2569(2569)"
ETS_CAPTURED_SYSTEM = "2"

# Resource pack specifics ----------------------------------------------------
ETS_ZIP_FOOTER_SIZE = 336
ETS_EVALUATE_RESOURCE_ID = "55"

# Disclaimer ----------------------------------------------------------------
DISCLAIMER_VERSION = "1"
DISCLAIMER_TEXT = """FastETS 免责声明 / Disclaimer

本软件仅供学习、研究与技术原理探究使用。

1. 仅用于学习原理
   FastETS 是一个技术研究项目，用于学习 E听说（讯飞）客户端与云端接口的通信
   原理、语音合成流程与自动化编排方法。任何使用者仅可将本软件用于学习原理，
   并须在使用结束后 24 小时内删除本软件及其全部副本（包括源码、编译产物、
   缓存、录制的音频与克隆音色数据）。

2. 滥用与我无关
   使用本软件造成的任何后果，包括但不限于账号被封禁、成绩异常、学校处罚、
   学术诚信问题或第三方索赔，均由使用者自行承担，与本软件的作者、贡献者、
   维护者及发布平台完全无关。本软件作者不对任何形式的滥用行为负责，也不
   为任何违规使用提供便利或技术支持。

3. 必须保证正常练习
   使用者必须保证自己仍然进行正常、真实的英语练习。本软件不能、也不应替代
   你真实的听说训练。请在完成作业后自行对照原文复述与订正，把工具当作
   "参考答案生成器"与"朗读练习器"，而不是"刷分器"。

4. 账号与数据
   你必须拥有科大讯飞 E听说平台的合法、正版使用权限。本软件不会收集、
   上传或共享你的账号数据到任何第三方服务器；账号口令、Token 与录制的
   音频仅保存在你本机的应用数据目录中。请自行承担使用本软件所带来的
   账号安全风险（包括但不限于与官方客户端异地登录导致的顶号）。

5. 声音与授权
   你只克隆你自己或已获得明确授权的声音。声音克隆得到的音色数据仅用于
   你本人的练习，请勿用于冒充他人。

在点击"我已阅读并同意"即表示你已完整阅读并接受以上全部条款。
如果你不同意，请立即删除本软件。"""

DISCLAIMER_TITLE = "使用前必读 · 免责声明"


def data_dir() -> Path:
    """Return the per-user application data directory (created on demand)."""
    override = os.environ.get("FASTETS_HOME")
    if override:
        base = Path(override).expanduser()
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    elif sys.platform == "win32":
        root = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        base = Path(root) / APP_NAME
    else:
        root = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        base = Path(root) / APP_NAME.lower()
    base.mkdir(parents=True, exist_ok=True)
    return base


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def papers_cache_dir() -> Path:
    path = cache_dir() / "papers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def audio_cache_dir() -> Path:
    path = cache_dir() / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voice_dir() -> Path:
    path = data_dir() / "voice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path() -> Path:
    return data_dir() / "state.json"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def disclaimer_path() -> Path:
    return data_dir() / "disclaimer.json"


DEFAULT_SETTINGS: Dict[str, Any] = {
    # voice / TTS
    "tts_provider": "minimax",       # minimax | edge | openai_compat | macos
    "tts_fallback": "macos",         # macos | edge | none - used when the main one fails
    "voice_mode": "clone",           # clone | default
    "default_voice": "",             # voice id used when voice_mode == default
    "minimax_api_key": "",
    "minimax_model": "speech-2.6-hd",
    "minimax_base_url": "https://api.minimaxi.com",
    "minimax_voice_id": "",
    "minimax_language_boost": "auto",
    "tts_speed": 1.0,
    "tts_pitch": 0,
    "edge_voice": "en-US-AriaNeural",
    "macos_voice": "Samantha",
    "openai_base_url": "",
    "openai_api_key": "",
    "openai_model": "tts-1",
    "openai_voice": "alloy",
    "openai_clone_endpoint": "",
    # 复述 / 表达：资源包里有多份示例答案，朗读时挑哪一份
    "answer_pick_mode": "first",     # first | random | manual
    # scoring
    "score_mode": "target",           # target | estimate
    "target_score_ratio": 1.0,        # fraction of the part maximum when score_mode == target
    # ETS 评分服务异常时的重试次数（答案已送达，只是分数没算出来）
    "mark_retries": 2,
    # runtime
    "ffmpeg_path": "",
    "dry_run": True,
    "skip_ssl_verify": False,
    "keep_audio_cache": True,
    # pacing / anti-throttle
    "pace_enabled": True,
    "request_delay_ms": 400,
    "request_jitter_ratio": 0.4,
    "reading_wpm": 150,
    "min_reading_delay_ms": 800,
    "max_reading_delay_ms": 9000,
    "section_pause_ms": 1500,
    # Optional explicit route that clears a previous answer before a redo.
    # Empty = redo by re-submitting (the sync endpoint overwrites the record).
    "redo_reset_route": "",
}


class JsonStore:
    """Small thread-safe JSON document store."""

    def __init__(self, path: Path, default: Any = None) -> None:
        self._path = path
        self._default = default if default is not None else {}
        # Reentrant: update() calls read() while holding the lock.
        self._lock = threading.RLock()

    def read(self) -> Any:
        with self._lock:
            if not self._path.exists():
                return json.loads(json.dumps(self._default))
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return json.loads(json.dumps(self._default))

    def write(self, data: Any) -> None:
        with self._lock:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)

    def update(self, mutator) -> Any:
        with self._lock:
            current = self.read()
            result = mutator(current)
            self.write(result)
            return result


def load_settings() -> Dict[str, Any]:
    store = JsonStore(settings_path(), {})
    saved = store.read() or {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    return merged


def save_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    merged = load_settings()
    merged.update({k: v for k, v in settings.items() if k in DEFAULT_SETTINGS})
    JsonStore(settings_path(), {}).write(merged)
    return merged


def disclaimer_accepted() -> bool:
    data = JsonStore(disclaimer_path(), {}).read() or {}
    return bool(data.get("accepted")) and data.get("version") == DISCLAIMER_VERSION


def accept_disclaimer() -> None:
    JsonStore(disclaimer_path(), {}).write(
        {"accepted": True, "version": DISCLAIMER_VERSION}
    )


def reset_disclaimer() -> None:
    """Force the disclaimer to show again on the next launch."""
    path = disclaimer_path()
    if path.exists():
        path.unlink()
