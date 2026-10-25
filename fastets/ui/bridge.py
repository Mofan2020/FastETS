"""The Python side of the pywebview bridge.

Every method here is callable from JavaScript as
``window.pywebview.api.<method>(...)`` and must return JSON serialisable data.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config
from ..core import completer as completer_mod
from ..core.ets_api import EtsApiError
from ..core.session import Session
from ..jobs.runner import JobRunner
from ..voice import ProviderError, build_provider, resolve_voice_id

# How long the run waits for the user to pick one of the sample answers
# before it continues with the first one.
CHOICE_TIMEOUT_SECONDS = 180

# Texts the user has to read for the voice clone. The English prompt is what
# MiniMax uses as `clone_prompt.prompt_audio` so the cloned voice keeps a
# natural English timbre.
CLONE_SAMPLE_ZH = (
    "同学们好，今天我要用一段中文来录制我的声音样本。"
    "请大家把这句话完整地朗读一遍，注意发音要清晰，语速保持自然。"
    "这段录音将用于训练一个只属于我自己的声音模型。"
)
CLONE_SAMPLE_EN = (
    "Good morning everyone. Today I am going to read a short English passage "
    "so that my voice model can learn my pronunciation, rhythm and intonation. "
    "Please listen carefully and speak at a natural pace."
)

CHANGYAN_LOGIN_URL = (
    "https://pass.changyan.com/login?nextpage=aHR0cHM6Ly93d3cuZXRzMTAwLmNvbS9sb2dpbkNoZWNrLmh0bWw="
    "&customConfig=e2hpZGRlbl9tb2R1bGU6ICJoZWFkZXIsdGFpbCxsb2dpbkJ5VmVyaWZ5Q29kZSxyZWdpc3Rlcixsb2dpbkJ5VGhpcmRMb2dpbiIscHJvZHVjdF9hcHBrZXk6InFpbmdkYW9fZXRzIiwibmVlZFRpY2tldCI6InRydWUiLCJsb2dpbl9ub3RBdXRvIjoidHJ1ZSJ9"
    "&from=ew&appId=pass6port18"
)

CHANGYAN_HOOK_JS = r"""
(function () {
  if (window.__fastetsInstalled) { return true; }
  window.__fastetsInstalled = true;
  function hook() {
    if (!window.$ || !window.jQuery) { window.setTimeout(hook, 250); return; }
    $(document).ajaxComplete(function (event, xhr, settings) {
      if (!settings || !settings.url) { return; }
      if (settings.url.indexOf('/login/checkLogin') === -1) { return; }
      try {
        var body = JSON.parse(xhr.responseText);
        if (body.Code !== 0) { return; }
        var data = (typeof body.Data === 'string') ? JSON.parse(body.Data) : body.Data;
        if (data && data.uid) {
          window.__fastetsLoginResult = {
            uid: String(data.uid),
            captchaResult: String(data.captchaResult || '')
          };
        }
      } catch (err) {}
    });
  }
  hook();
  return true;
})();
"""


class FastETSApi:
    """API object exposed to the web view."""

    def __init__(self, logger=None, window=None) -> None:
        self._log = logger
        self.session = Session(logger=logger)
        self.runner = JobRunner(logger=logger)
        self._window = window
        self._pending_accounts: List[Dict[str, Any]] = []
        self._changyan_window = None
        self._changyan_result: Optional[Dict[str, str]] = None
        self._changyan_started_at = 0.0
        self._lock = threading.Lock()
        self._choose_lock = threading.Lock()
        self._choose_request: Optional[Dict[str, Any]] = None
        self._choose_event: Optional[threading.Event] = None
        self._choose_choice: Optional[str] = None

    # -- helpers -------------------------------------------------------------

    def _completer(self, progress=None) -> completer_mod.Completer:
        return completer_mod.Completer(
            self.session,
            settings=config.load_settings(),
            logger=self._log,
            progress=progress,
            chooser=self._ask_user_choice,
        )

    def _set_window(self, window) -> None:
        self._window = window

    # -- manual answer choice -------------------------------------------------

    def _ask_user_choice(self, request: Dict[str, Any]) -> str:
        """Ask the user which of the sample answers should be read.

        The run waits on a worker thread while the UI shows the options; if
        nobody answers in time the first option is used so the run keeps
        going.
        """
        key = str(request.get("key") or "")
        options = [str(item) for item in (request.get("options") or [])]
        label = str(request.get("label") or "")
        if not options:
            return ""

        with self._choose_lock:
            event = threading.Event()
            self._choose_event = event
            self._choose_request = {"key": key, "label": label, "options": options}
            self._choose_choice = None

        self.runner.progress(
            {
                "stage": "choose",
                "message": f"请选择要朗读的示例答案：{label}",
                "choose": {
                    "key": key,
                    "label": label,
                    "options": options,
                },
            }
        )
        answered = event.wait(timeout=CHOICE_TIMEOUT_SECONDS)
        with self._choose_lock:
            choice = self._choose_choice
            self._choose_event = None
            self._choose_request = None
            self._choose_choice = None
        if not answered or choice is None:
            if self._log:
                self._log.info("选择答案超时，改用第一个示例答案: %s", label)
            return options[0]
        return choice

    def choose_answer(self, key: str, index: int) -> Dict[str, Any]:
        """Called by the UI when the user picked one of the sample answers."""
        with self._choose_lock:
            request = self._choose_request or {}
            event = self._choose_event
            if not request or event is None:
                return self._fail("当前没有等待选择的问题")
            if str(request.get("key") or "") != str(key or ""):
                return self._fail("选择请求已过期")
            options = list(request.get("options") or [])
            try:
                position = int(index)
            except (TypeError, ValueError):
                position = -1
            if position < 0 or position >= len(options):
                return self._fail("选项序号无效")
            self._choose_choice = options[position]
        event.set()
        return self._ok(chosen=self._choose_choice)

    def _ok(self, **extra: Any) -> Dict[str, Any]:
        payload = {"ok": True}
        payload.update(extra)
        return payload

    def _fail(self, message: str, **extra: Any) -> Dict[str, Any]:
        payload = {"ok": False, "message": str(message)}
        payload.update(extra)
        return payload

    # -- bootstrap -----------------------------------------------------------

    def boot(self) -> Dict[str, Any]:
        """Called once when the UI loads."""
        settings = config.load_settings()
        return self._ok(
            version=__import__("fastets").__version__,
            disclaimer_required=not config.disclaimer_accepted(),
            disclaimer_text=config.DISCLAIMER_TEXT,
            disclaimer_title=config.DISCLAIMER_TITLE,
            logged_in=self.session.is_logged_in,
            phone=self.session.phone,
            account=self.session._read("selected_ecard_name", ""),
            grade=self.session._read("selected_ecard_grade", ""),
            device_code=self.session.device_code,
            settings=settings,
            provider_names={
                "minimax": "MiniMax 声音克隆 + T2A",
                "openai_compat": "OpenAI 兼容接口",
                "edge": "Microsoft Edge TTS（免密钥）",
            },
        )

    def accept_disclaimer(self) -> Dict[str, Any]:
        config.accept_disclaimer()
        return self._ok()

    def reopen_disclaimer(self) -> Dict[str, Any]:
        config.reset_disclaimer()
        return self._ok()

    # -- settings ------------------------------------------------------------

    def get_settings(self) -> Dict[str, Any]:
        return self._ok(settings=config.load_settings())

    def save_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        saved = config.save_settings(settings or {})
        self.session.set_protocol(str(saved.get("protocol") or self.session.protocol))
        return self._ok(settings=saved)

    # -- authentication ------------------------------------------------------

    def login(
        self, phone: str, password: str, remember_password: bool = True
    ) -> Dict[str, Any]:
        """Log in. Returns the usable accounts when more than one exists."""
        try:
            result = self.session.authenticate(phone, password, remember_password)
        except EtsApiError as exc:
            return self._fail(exc)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        usable = result.get("usable") or []
        if not usable:
            return self._fail(
                "该账号下没有可用年级/班级（E卡可能已过期），请先在官方 App 续费或激活"
            )
        if len(usable) == 1:
            activated = self.session.activate_account(usable[0])
            return self._ok(
                account=activated["account"].get("name", ""),
                grade=activated["account"].get("grade", ""),
            )
        return self._ok(need_account=True, accounts=usable)

    def select_account(self, account_id: str) -> Dict[str, Any]:
        """Activate one of the E-cards returned by :meth:`login`."""
        try:
            usable = getattr(self.session, "_usable_accounts", [])
            chosen = next(
                (a for a in usable if a.get("id") == account_id or a.get("parent_id") == account_id),
                None,
            )
            if chosen is None:
                return self._fail("账号不存在")
            activated = self.session.activate_account(chosen)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(
            account=activated["account"].get("name", ""),
            grade=activated["account"].get("grade", ""),
        )

    def logout(self) -> Dict[str, Any]:
        self.session.clear()
        return self._ok()

    def import_token(self, token: str) -> Dict[str, Any]:
        try:
            result = self.session.import_token(token.strip())
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(account=result["account"].get("name", ""))
    # -- changyan (畅言) web login -------------------------------------------

    def changyan_login_start(self) -> Dict[str, Any]:
        try:
            import webview
        except ImportError:
            return self._fail("pywebview 未安装")
        if self._changyan_window is not None:
            try:
                self._changyan_window.destroy()
            except Exception:
                pass
        self._changyan_result = None
        self._changyan_started_at = time.time()
        try:
            window = webview.create_window(
                "畅言登录 · FastETS",
                url=CHANGYAN_LOGIN_URL,
                width=980,
                height=760,
                resizable=True,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"无法打开登录窗口: {exc}")
        self._changyan_window = window

        def on_loaded() -> None:
            try:
                window.evaluate_js(CHANGYAN_HOOK_JS)
            except Exception:
                pass

        window.events.loaded += on_loaded
        return self._ok()

    def changyan_login_poll(self) -> Dict[str, Any]:
        """Check whether the passport login produced a token."""
        if self._changyan_result:
            return self._ok(result=self._changyan_result)
        window = self._changyan_window
        if window is None:
            return self._fail("登录窗口未打开")
        try:
            raw = window.evaluate_js("window.__fastetsLoginResult ? JSON.stringify(window.__fastetsLoginResult) : null")
        except Exception:
            raw = None
        if not raw:
            if time.time() - self._changyan_started_at > 300:
                return self._fail("登录超时")
            return self._ok(pending=True)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return self._ok(pending=True)
        self._changyan_result = {
            "uid": str(data.get("uid", "")),
            "captchaResult": str(data.get("captchaResult", "")),
        }
        try:
            result = self.session.login_with_changyan(
                self._changyan_result["uid"],
                self._changyan_result["captchaResult"],
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        try:
            window.destroy()
        except Exception:
            pass
        self._changyan_window = None
        usable = result.get("usable") or []
        if usable and not result.get("account"):
            return self._ok(result=self._changyan_result, need_account=True, accounts=usable)
        return self._ok(result=self._changyan_result, logged_in=True)

    # -- homework ------------------------------------------------------------

    def list_homework(self, status: str = "1") -> Dict[str, Any]:
        """List assignments. ``status`` may be ``all`` to merge every state."""
        try:
            completer = self._completer()
            if status == "all":
                result = completer.list_all_homework()
            elif status == "1":
                result = completer.list_all_pending()
            else:
                result = completer.list_homework(status)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(result=result)

    def load_paper(self, homework: Dict[str, Any]) -> Dict[str, Any]:
        try:
            completer = self._completer()
            paper = completer.load_paper(homework)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(paper=paper.to_dict())

    def start_complete(
        self,
        homeworks: List[Dict[str, Any]],
        dry_run: Optional[bool] = None,
        redo: bool = False,
    ) -> Dict[str, Any]:
        if not homeworks:
            return self._fail("请先选择要完成的作业")
        settings = config.load_settings()
        if dry_run is None:
            dry_run = bool(settings.get("dry_run", True))
        completer = self._completer(progress=self.runner.progress)

        def run() -> Dict[str, Any]:
            reports: List[Dict[str, Any]] = []
            for homework in homeworks:
                self.runner.log(
                    f"{'重做' if redo else '开始处理'}作业：{homework.get('name', '')}"
                )
                if redo:
                    report = completer.redo_homework(homework, dry_run=dry_run)
                else:
                    report = completer.complete_homework(homework, dry_run=dry_run)
                reports.append(report)
            return {"reports": reports, "mode": "redo" if redo else "complete"}

        try:
            job = self.runner.start(
                "redo" if redo else "complete",
                "重做作业" if redo else "完成作业",
                run,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(job_id=job.id)

    def stop_job(self) -> Dict[str, Any]:
        stopped = self.runner.stop()
        return self._ok(stopped=stopped)

    def poll_events(self, seq: int = 0) -> Dict[str, Any]:
        return self._ok(**self.runner.events_since(int(seq or 0)))

    # -- voice ---------------------------------------------------------------

    def voice_samples(self) -> Dict[str, Any]:
        return self._ok(
            zh=CLONE_SAMPLE_ZH,
            en=CLONE_SAMPLE_EN,
            zh_path=str(config.voice_dir() / "sample_zh"),
            en_path=str(config.voice_dir() / "sample_en"),
        )

    def save_recording(self, data_b64: str, kind: str) -> Dict[str, Any]:
        """Persist an imported audio file as the voice clone sample.

        The sample is converted to a 16 kHz mono mp3 (what every voice service
        accepts) and the result is probed so that the UI can show what was
        actually imported - duration, format and loudness.
        """
        if kind not in ("zh", "en"):
            return self._fail("未知的样本类型")
        if not data_b64:
            return self._fail("没有收到音频数据，请重新导入文件")
        try:
            payload = base64.b64decode(data_b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            return self._fail(f"音频数据解析失败: {exc}")
        if len(payload) < 1024:
            return self._fail(
                f"音频文件太小（{len(payload)} 字节），请导入一段完整的录音"
            )

        from ..core.audio import AudioTools

        tools = AudioTools(logger=self._log)
        raw = config.voice_dir() / f"sample_{kind}.input"
        raw.write_bytes(payload)
        try:
            info = tools.probe(raw)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"无法检查音频文件: {exc}")
        if not info.get("duration_ms"):
            return self._fail(
                "无法识别这个音频文件（ffmpeg 解码失败）。"
                "请导入 mp3 / wav / m4a / flac 等常见格式。"
            )
        if info["duration_ms"] < 300:
            return self._fail(
                f"音频太短（{info['duration_ms'] / 1000:.2f} 秒），"
                "请录制至少 1 秒以上的完整朗读"
            )

        mp3 = config.voice_dir() / f"sample_{kind}.mp3"
        try:
            tools.to_mp3(raw, mp3)
        except Exception as exc:  # noqa: BLE001
            return self._fail(f"音频转码失败: {exc}")

        volume = tools.mean_volume_db(mp3)
        info["mean_volume_db"] = volume
        info["path"] = str(mp3)
        warning = ""
        if volume is not None and volume <= -45.0:
            warning = (
                f"这段录音几乎没有声音（平均音量 {volume:.1f} dB），"
                "克隆出来的音色会是空的，请重新录制"
            )
        return self._ok(path=str(mp3), info=info, warning=warning)

    def clone_voice(self) -> Dict[str, Any]:
        zh = config.voice_dir() / "sample_zh.mp3"
        en = config.voice_dir() / "sample_en.mp3"
        if not zh.exists() or zh.stat().st_size == 0:
            return self._fail("请先导入中文样本音频文件")
        completer = self._completer(progress=self.runner.progress)

        def run() -> Dict[str, Any]:
            return completer.clone_voice(
                zh,
                en if en.exists() and en.stat().st_size else None,
                CLONE_SAMPLE_EN,
                "FastETSVoice",
            )

        try:
            job = self.runner.start("clone", "克隆声音", run)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(job_id=job.id)

    def test_tts(self, text: str = "") -> Dict[str, Any]:
        """Synthesize a short sample and hand the audio back to the browser."""
        settings = config.load_settings()
        content = text or "Hello, this is a test of the FastETS speech engine."
        try:
            provider = build_provider(settings, logger=self._log)
            voice_id = resolve_voice_id(settings, provider)
            result = provider.synthesize(
                content,
                voice_id=voice_id,
                speed=float(settings.get("tts_speed", 1.0) or 1.0),
                pitch=int(settings.get("tts_pitch", 0) or 0),
            )
        except (ProviderError, Exception) as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok(
            audio=base64.b64encode(result.audio).decode("ascii"),
            mime="audio/mpeg",
            voice_id=voice_id,
        )

    def list_voices(self) -> Dict[str, Any]:
        settings = config.load_settings()
        try:
            provider = build_provider(settings, logger=self._log)
            return self._ok(voices=provider.list_voices())
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)

    def check_environment(self) -> Dict[str, Any]:
        """Verify ffmpeg, the Speex encoder and the configured voice provider."""
        from ..core.audio import AudioTools, resolve_ffmpeg

        result: Dict[str, Any] = {"ok": True, "ffmpeg": "", "provider": {}, "speex": {}}
        try:
            result["ffmpeg"] = resolve_ffmpeg(str(config.load_settings().get("ffmpeg_path") or ""))
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["ffmpeg"] = str(exc)
        try:
            result["speex"] = AudioTools(logger=self._log).speex_status()
        except Exception as exc:  # noqa: BLE001
            result["speex"] = {"available": False, "method": "none", "message": str(exc)}
        try:
            provider = build_provider(config.load_settings(), logger=self._log)
            result["provider"] = provider.health_check()
        except Exception as exc:  # noqa: BLE001
            result["ok"] = False
            result["provider"] = {"ok": False, "message": str(exc)}
        return self._ok(**result)

    def open_path(self, path: str) -> Dict[str, Any]:
        target = Path(path)
        if not target.exists():
            return self._fail("路径不存在")
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif os.uname().sysname == "Darwin":
                os.system(f'open "{target}"')
            else:
                os.system(f'xdg-open "{target}"')
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc)
        return self._ok()
