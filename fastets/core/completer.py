"""End-to-end automation of one assignment.

The flow per assignment is:

1. download the resource pack of every 大题 from the CDN
2. decrypt it and parse ``content.json``
3. for every question: synthesise the required speech, encode it, upload it
   to OSS and report the result back to the server
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from .. import config
from ..voice import (
    ProviderError,
    VoiceProvider,
    build_fallback_provider,
    build_provider,
    resolve_voice_id,
)
from . import audio as audio_mod
from . import crypto, oss, paper as paper_mod, score as score_mod, zipcrypto
from .ets_api import CODE_TOKEN_EXPIRED, EtsApiClient, EtsApiError
from .pacing import Pacer, pacer_from_settings
from .session import Session

ProgressCallback = Callable[[Dict[str, Any]], None]
# The UI hands the completer a callback that asks the user to pick one of the
# sample answers of a 复述 / 表达 question.
AnswerChooser = Callable[[Dict[str, Any]], str]

# ETS sometimes cannot reach its own marking service after the answer was
# delivered.  The recording, the spx and the score XML are already stored, only
# the score is missing, so the run retries and then reports the question as
# "submitted, not confirmed" instead of a hard failure.
MARK_RETRY_DELAY_SECONDS = 4
_TRANSIENT_MARK_ERRORS = (
    "远程获取ets服务失败",
    "获取ets服务失败",
    "服务繁忙",
    "系统繁忙",
    "服务暂时不可用",
)


def _is_transient_mark_error(exc: Exception) -> bool:
    """True for the ETS errors that mean "the answer is in, the score is not"."""
    if not isinstance(exc, EtsApiError):
        return False
    message = str(exc)
    return any(marker in message for marker in _TRANSIENT_MARK_ERRORS)


class CancelledError(RuntimeError):
    """Raised when the user stops a running job."""


class LoadedPaper:
    """A parsed assignment: the 大题 and the questions inside them."""

    def __init__(self, homework: Dict[str, Any], sections: List[paper_mod.Section]) -> None:
        self.homework = homework
        self.sections = sections

    @property
    def questions(self) -> List[paper_mod.Question]:
        return [question for section in self.sections for question in section.questions]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.homework.get("id", ""),
            "name": self.homework.get("name", ""),
            "sections": [section.to_dict() for section in self.sections],
        }


class Completer:
    """Drives one complete assignment run."""

    def __init__(
        self,
        session: Session,
        settings: Optional[Dict[str, Any]] = None,
        logger=None,
        progress: Optional[ProgressCallback] = None,
        chooser: Optional[AnswerChooser] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self.session = session
        self.settings = settings if settings is not None else config.load_settings()
        self._log = logger
        self._progress_cb = progress
        self._chooser = chooser
        self._cancel_event = cancel_event
        # The server keys an answer record by "resource pack + item number".
        # The counter restarts for every pack (one section = one pack).
        self._serial_cursor = 0
        self.pacer: Pacer = pacer_from_settings(self.settings, stop_event=cancel_event)
        self._provider: Optional[VoiceProvider] = None
        self._audio = audio_mod.AudioTools(
            ffmpeg_path=str(self.settings.get("ffmpeg_path") or ""), logger=logger
        )

    # -- plumbing -----------------------------------------------------------

    @property
    def provider(self) -> VoiceProvider:
        if self._provider is None:
            self._provider = build_provider(self.settings, logger=self._log)
        return self._provider

    @property
    def client(self) -> EtsApiClient:
        return self.session.client

    def _emit(self, stage: str, message: str, percent: Optional[float] = None, **extra: Any) -> None:
        if self._progress_cb is None:
            return
        payload = {"stage": stage, "message": message, "percent": percent, "time": time.time()}
        payload.update(extra)
        try:
            self._progress_cb(payload)
        except Exception:  # never let a UI callback kill the run
            pass

    def _info(self, message: str, *args: Any) -> None:
        if self._log:
            self._log.info(message, *args)

    def _warn_mark_once(self, reason: str) -> None:
        """Tell the user once that ETS could not confirm a submitted answer."""
        if getattr(self, "_mark_warned", False):
            return
        self._mark_warned = True
        self._info("ETS 评分服务异常（答案已提交）: %s", reason)

    def _warn_speex_once(self, reason: str) -> None:
        """Tell the user once that the optional spx attachment is missing."""
        if getattr(self, "_speex_warned", False):
            return
        self._speex_warned = True
        self._info("Speex 附件未附带: %s", reason)
        self._emit(
            "error",
            f"提示：{reason}（本次未附带 spx 附件，mp3 与评测 XML 仍会照常上传，分数不受影响）",
        )

    def _check_cancel(self) -> None:
        """Raise when the user pressed "stop".

        Both the pacer and the job runner's cancel flag are honoured: the
        pacer owns the interruptible sleeps, the runner flag catches the
        moments in between (uploads, synthesis, requests).
        """
        if self.pacer.stopped or (
            self._cancel_event is not None and self._cancel_event.is_set()
        ):
            raise CancelledError("任务已被用户取消")

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake up immediately when the run is cancelled."""
        if seconds <= 0:
            self._check_cancel()
            return
        if self._cancel_event is not None:
            if self._cancel_event.wait(seconds):
                self._check_cancel()
            return
        time.sleep(seconds)

    # -- listing ------------------------------------------------------------

    def list_homework(self, status: str = "1") -> Dict[str, Any]:
        token = self.session.ensure_fresh_token()
        parent_id = self.session.parent_account_id
        if not parent_id:
            raise EtsApiError("缺少 parent_account_id，请重新登录")
        self.pacer.request()
        try:
            result = self.client.homework_list(token, parent_id, status=status)
        except EtsApiError as exc:
            if exc.code == CODE_TOKEN_EXPIRED and self._relogin():
                self.pacer.request()
                result = self.client.homework_list(
                    self.session.token, self.session.parent_account_id, status=status
                )
            else:
                raise
        for homework in result.get("homeworks", []):
            homework.setdefault("base_url", result.get("base_url") or config.ETS_CDN_BASE_URL)
        return result

    def _relogin(self) -> bool:
        """Re-login once when the stored token expired."""
        phone, password = self.session.phone, self.session.password
        if not phone or not password:
            return False
        self._info("token 已过期，尝试重新登录")
        try:
            self.session.login_with_password(phone, password)
            return True
        except Exception as exc:  # noqa: BLE001
            self._info("重新登录失败: %s", exc)
            return False

    def list_all_pending(self) -> Dict[str, Any]:
        """Every assignment that still has to be done.

        作业集 (homework sets) are returned by the server as a parent entry;
        their children are included as well, so the UI can show a flat list.
        """
        result = self.list_homework("1")
        flat: List[Dict[str, Any]] = []
        for homework in result.get("homeworks", []):
            flat.append(homework)
            children = homework.get("children")
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        merged = dict(child)
                        merged.setdefault("name", homework.get("name", ""))
                        merged["parent_name"] = homework.get("name", "")
                        flat.append(merged)
        result["homeworks"] = flat
        return result

    def list_all_homework(self) -> Dict[str, Any]:
        """Every assignment regardless of state: 未完成 / 进行中 / 已完成.

        All three states are needed because a 重做 (redo) must be possible for
        assignments that already reached the passing mark as well.
        """
        merged: List[Dict[str, Any]] = []
        seen = set()
        base_url = config.ETS_CDN_BASE_URL
        for status in ("1", "2", "3"):
            try:
                self.pacer.request()
                result = self.client.homework_list(
                    self.session.ensure_fresh_token(),
                    self.session.parent_account_id,
                    status=status,
                )
            except EtsApiError as exc:
                if not merged:
                    raise
                self._info("作业列表 status=%s 拉取失败: %s", status, exc)
                continue
            base_url = result.get("base_url") or base_url
            for homework in result.get("homeworks", []):
                key = (homework.get("id"), homework.get("group_name"))
                if key in seen:
                    continue
                seen.add(key)
                homework = dict(homework)
                homework["status"] = status
                homework["base_url"] = base_url
                merged.append(homework)
        return {"base_url": base_url, "homeworks": merged, "status": "all"}

    def redo_homework(
        self,
        homework: Dict[str, Any],
        dry_run: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Redo an assignment - allowed no matter whether it already passed.

        The ETS100 cloud has no dedicated "clear answer" route; an answer record
        is keyed by ``homework_id`` + ``order`` + ``serial_id`` and the sync
        endpoint overwrites it. Re-running the pipeline therefore replaces the
        previous attempt. When ``redo_reset_route`` is configured in the
        settings, that route is called first so the server can drop the old
        record explicitly.
        """
        if dry_run is None:
            dry_run = bool(self.settings.get("dry_run", True))
        reset_route = str(self.settings.get("redo_reset_route") or "").strip()
        if reset_route and not dry_run:
            self._info("重做：先调用重置路由 %s", reset_route)
            self.pacer.request()
            try:
                self.client.request(
                    reset_route,
                    {
                        "sn": crypto.create_request_sn(),
                        "token": self.session.token,
                        "homework_id": homework.get("id", ""),
                        "set_id": homework.get("set_id", ""),
                        "version": config.ETS_DEFAULT_VERSION,
                        "system": config.ETS_DEFAULT_SYSTEM,
                        "global_client_version": config.ETS_DEFAULT_GLOBAL_CLIENT_VERSION,
                        "sign_response": 1,
                    },
                    allow_codes=[0],
                )
            except EtsApiError as exc:
                self._info("重置路由返回: %s（继续用重新提交方式重做）", exc)

        report = self.complete_homework(homework, dry_run=dry_run)
        report["mode"] = "redo"
        return report

    # -- paper loading -------------------------------------------------------

    def load_paper(self, homework: Dict[str, Any]) -> LoadedPaper:
        """Download and parse every resource pack of one assignment."""
        base_url = str(
            homework.get("base_url") or config.ETS_CDN_BASE_URL
        )
        contents = homework.get("contents") or []
        if not contents:
            raise EtsApiError(f"作业「{homework.get('name')}」没有可下载的内容")

        sections: List[paper_mod.Section] = []
        offsets: Dict[str, int] = {}
        set_id = str(homework.get("set_id") or "")
        homework_id = str(homework.get("id") or "")
        # The marking service is looked up by resource_id.  It is a property of
        # the assignment (the homework list carries it), not of the question, so
        # it has to be handed down to every question of this assignment.
        resource_id = str(
            homework.get("resource_id")
            or (homework.get("raw") or {}).get("resource_id")
            or ""
        )
        if not resource_id:
            resource_id = self._lookup_resource_id(homework_id, token=None)

        for index, content in enumerate(contents):
            self._check_cancel()
            group_name = str(content.get("group_name") or "")
            url = _join_url(base_url, str(content.get("url") or ""))
            if not url:
                continue
            self._emit(
                "download",
                f"下载资源包 {index + 1}/{len(contents)}：{group_name}",
                percent=round(index / max(len(contents), 1) * 100),
                group=group_name,
            )
            self.pacer.request()
            data = self._download(url)

            target = config.papers_cache_dir() / f"{crypto.md5_hex(url)}"
            try:
                zipcrypto.extract_resource_pack(data, target)
            except zipcrypto.ResourcePackError:
                if target.exists():
                    for child in target.iterdir():
                        if child.is_dir():
                            import shutil

                            shutil.rmtree(child, ignore_errors=True)
                zipcrypto.extract_resource_pack(data, target)

            payload = zipcrypto.read_content_json(target)
            offset = offsets.get(group_name, 0)
            section = paper_mod.parse_content_json(
                payload,
                homework_id,
                str(content.get("url") or ""),
                group_name,
                context_base={"set_id": set_id, "resource_id": resource_id},
                order_offset=offset,
            )
            sections.append(section)
            offsets[group_name] = offset + len(section.questions)
            structure_type = str(payload.get("structure_type") or "")
            if not paper_mod.is_known_structure(structure_type):
                # Worth a log line: the type name is what is needed to write an
                # exact parser for this pack shape.
                self._info(
                    "未识别的题型结构 %s（%s），已按通用规则拆成 %d 道题",
                    structure_type or "(空)",
                    group_name or "未命名大题",
                    len(section.questions),
                )

        return LoadedPaper(homework, sections)

    def _lookup_resource_id(self, homework_id: str, token=None) -> str:
        """Find the ``resource_id`` of an assignment in the homework list.

        The id is what the server uses to look up the marking service, so a
        missing one has to be recovered instead of falling back to the generic
        default - otherwise every submission fails.
        """
        if not homework_id:
            return ""
        try:
            api_token = token or self.session.token or self.session.ensure_fresh_token()
        except EtsApiError:
            return ""
        for status in ("1", "2", "3"):
            try:
                result = self.client.homework_list(
                    api_token, self.session.parent_account_id, status=status
                )
            except EtsApiError:
                continue
            for item in result.get("homeworks") or []:
                if str(item.get("id")) == str(homework_id):
                    found = str(
                        item.get("resource_id")
                        or (item.get("raw") or {}).get("resource_id")
                        or ""
                    )
                    if found:
                        self._info("作业 %s 的 resource_id = %s", homework_id, found)
                        return found
        return ""

    def _download(self, url: str) -> bytes:
        cached = zipcrypto.cache_path_for(url)
        if cached.exists() and cached.stat().st_size > 1024:
            return cached.read_bytes()
        headers = {"User-Agent": config.ETS_USER_AGENT, "Accept": "*/*"}
        verify = not bool(self.settings.get("skip_ssl_verify", False))
        try:
            response = requests.get(
                url, headers=headers, timeout=60, verify=verify, stream=True
            )
        except requests.RequestException as exc:
            raise EtsApiError(f"资源包下载失败: {exc}") from exc
        if response.status_code >= 400:
            raise EtsApiError(f"资源包下载 HTTP {response.status_code}: {url}")
        payload = response.content
        try:
            cached.write_bytes(payload)
        except OSError:
            pass
        return payload

    # -- scoring -------------------------------------------------------------

    def _voice_for(self, provider: VoiceProvider) -> str:
        """The voice identifier that belongs to the active provider."""
        return resolve_voice_id(self.settings, provider)

    def _fallback_provider(self) -> Optional[VoiceProvider]:
        """The backup provider used when the configured one fails."""
        if getattr(self, "_fallback", None) is not None:
            return self._fallback
        try:
            self._fallback = build_fallback_provider(self.settings, logger=self._log)
        except Exception as exc:  # noqa: BLE001 - the fallback is optional
            self._info("备用语音服务不可用: %s", exc)
            self._fallback = None
        return self._fallback

    def _synthesize(
        self, text: str, label: str, question: paper_mod.Question
    ) -> Any:
        """Synthesise a text, falling back to the backup service if needed."""
        speed = float(self.settings.get("tts_speed", 1.0) or 1.0)
        pitch = int(self.settings.get("tts_pitch", 0) or 0)
        try:
            return self.provider.synthesize(
                text,
                voice_id=self._voice_for(self.provider),
                speed=speed,
                pitch=pitch,
            )
        except Exception as exc:  # noqa: BLE001 - the fallback may still work
            fallback = self._fallback_provider()
            if fallback is None:
                raise
            self._info(
                "主语音服务失败，改用备用服务 %s: %s", fallback.name, exc
            )
            self._emit(
                "warn",
                f"主语音服务失败，改用备用服务（{fallback.name}）：{exc}",
                group=question.group_name,
            )
            return fallback.synthesize(
                text,
                voice_id=self._voice_for(fallback),
                speed=speed,
                pitch=pitch,
            )

    def _score_for(
        self, question: paper_mod.Question, segment_max: float, text: str, duration_ms: int
    ) -> float:
        mode = str(self.settings.get("score_mode") or "target")
        if mode == "estimate":
            return score_mod.estimate_score(text, duration_ms, segment_max)
        ratio = float(self.settings.get("target_score_ratio", 1.0) or 1.0)
        return max(0.0, min(segment_max, segment_max * ratio))

    # -- submission ----------------------------------------------------------

    def complete_homework(
        self,
        homework: Dict[str, Any],
        paper: Optional[LoadedPaper] = None,
        dry_run: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Complete one assignment and return a per-question report."""
        if dry_run is None:
            dry_run = bool(self.settings.get("dry_run", True))
        token = self.session.token or self.session.ensure_fresh_token()
        paper = paper or self.load_paper(homework)

        total_questions = len(paper.questions)
        if not total_questions:
            raise EtsApiError("该作业没有解析出任何题目")

        self.pacer.reset()
        self._emit("start", f"开始完成作业：{homework.get('name', '')}", percent=0)
        oss_config = self.client.oss_upload_config(token)
        uploader = oss.OssUploader(oss_config, logger=self._log)

        report: Dict[str, Any] = {
            "homework_id": homework.get("id", ""),
            "homework_name": homework.get("name", ""),
            "dry_run": dry_run,
            "total": total_questions,
            "done": 0,
            "failed": 0,
            "unconfirmed": 0,
            "items": [],
        }

        completed = 0
        for section_index, section in enumerate(paper.sections):
            self._check_cancel()
            self._serial_cursor = 0
            if section_index > 0:
                self.pacer.section()
            section_max = paper_mod.part_max_score(section.group_name)
            per_question_max = section_max / max(len(section.questions), 1)

            for question in section.questions:
                self._check_cancel()
                percent = round(completed / max(total_questions, 1) * 100)
                label = f"{section.group_name} 第 {question.order} 题"
                self._emit(
                    "question",
                    f"处理中：{label}",
                    percent=percent,
                    group=section.group_name,
                    order=question.order,
                )
                try:
                    item = self._complete_question(
                        token, uploader, question, per_question_max, label, dry_run
                    )
                    report["items"].append(item)
                    report["done"] += 1
                    if item.get("status") == "submitted-unconfirmed":
                        report["unconfirmed"] += 1
                    completed += 1
                except CancelledError:
                    raise
                except Exception as exc:
                    report["failed"] += 1
                    completed += 1
                    report["items"].append(
                        {
                            "group": section.group_name,
                            "order": question.order,
                            "kind": question.kind,
                            "status": "failed",
                            "message": str(exc),
                        }
                    )
                    self._emit("error", f"{label} 失败：{exc}", group=section.group_name)
                    self._info("题目失败 %s: %s", label, exc)

        report["status"] = "completed" if report["failed"] == 0 else "partial"
        self._emit(
            "done",
            f"作业「{report['homework_name']}」完成 {report['done']}/{report['total']}"
            + (f"，失败 {report['failed']} 题" if report["failed"] else "")
            + (
                f"，{report['unconfirmed']} 题已提交但 ETS 评分未确认"
                if report["unconfirmed"]
                else ""
            ),
            percent=100,
            report=report,
        )
        return report

    def _complete_question(
        self,
        token: str,
        uploader: oss.OssUploader,
        question: paper_mod.Question,
        per_question_max: float,
        label: str,
        dry_run: bool,
    ) -> Dict[str, Any]:
        if question.is_choice:
            return self._complete_choice(token, question, per_question_max, label, dry_run)

        override = self._chosen_answer_text(question, label)
        segments = question.speech_segments(override)
        if not segments:
            return {
                "group": question.group_name,
                "order": question.order,
                "kind": question.kind,
                "status": "skipped",
                "message": "没有可朗读的文本",
            }

        segment_max = per_question_max / len(segments)
        results: List[Dict[str, Any]] = []
        for segment_index, text in enumerate(segments):
            self._check_cancel()
            results.append(
                self._submit_audio_segment(
                    token,
                    uploader,
                    question,
                    text,
                    segment_max,
                    segment_index,
                    len(segments),
                    label,
                    dry_run,
                )
            )
        unconfirmed = any(
            item.get("status") == "submitted-unconfirmed" for item in results
        )
        return {
            "group": question.group_name,
            "order": question.order,
            "kind": question.kind,
            "status": "submitted-unconfirmed" if unconfirmed else "submitted",
            "segments": results,
            "score": sum(item.get("score", 0.0) for item in results),
        }

    def _chosen_answer_text(self, question: paper_mod.Question, label: str) -> str:
        """Pick the sample answer that should be read out loud.

        复述 / 表达 ship several sample answers in the resource pack.  The
        setting decides which one is used: the first one, a random one, or
        the one the user picks while the run is waiting.
        """
        candidates = list(question.answer_candidates or [])
        if not candidates:
            return ""
        if len(candidates) == 1:
            return candidates[0]

        mode = str(self.settings.get("answer_pick_mode") or "first").strip().lower()
        chosen = candidates[0]
        if mode == "random":
            chosen = random.choice(candidates)
        elif mode == "manual" and self._chooser is not None:
            try:
                picked = self._chooser(
                    {
                        "key": f"{question.homework_id}:{question.id}",
                        "label": label,
                        "options": candidates,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - never break the run
                self._info("选择答案失败，改用第一个示例答案: %s", exc)
                picked = None
            if picked in candidates:
                chosen = picked
        try:
            index = candidates.index(chosen)
        except ValueError:
            index = 0
        self._info(
            "朗读第 %d/%d 个示例答案：%s", index + 1, len(candidates), chosen[:40]
        )
        return chosen

    def _next_serial_id(self) -> str:
        """The item number of the next recording inside the current pack.

        The server keys an answer record by "resource pack + item number".
        Every recording of one pack therefore needs its own number - otherwise
        several questions are merged into a single record and the assignment
        never reaches 100%.
        """
        serial = str(self._serial_cursor)
        self._serial_cursor += 1
        return serial

    def _submit_audio_segment(
        self,
        token: str,
        uploader: oss.OssUploader,
        question: paper_mod.Question,
        text: str,
        segment_max: float,
        segment_index: int,
        segment_count: int,
        label: str,
        dry_run: bool,
    ) -> Dict[str, Any]:
        # Pace like a human: read the text first, then upload.
        self.pacer.reading(text)

        self._emit("tts", f"合成语音：{label}", group=question.group_name)
        result = self._synthesize(text, label, question)

        raw_path = config.audio_cache_dir() / f"{crypto.random_hex(16)}.raw"
        raw_path.write_bytes(result.audio)

        mp3_path = self._audio.to_mp3(raw_path)
        duration_ms = result.duration_ms or self._audio.duration_ms(mp3_path)

        category = score_mod.category_for(question.structure_type, question.group_name)
        target_score = self._score_for(question, segment_max, text, duration_ms)
        score_value = score_mod.clamp_score(target_score, segment_max)
        xml_text = score_mod.build_score_xml(text, category, target_score)
        xml_path = config.audio_cache_dir() / f"{crypto.random_hex(16)}.xml"
        xml_path.write_text(xml_text, encoding="utf-8")

        if dry_run:
            if not self.settings.get("keep_audio_cache", True):
                _cleanup(raw_path, mp3_path, xml_path)
            return {
                "index": segment_index,
                "status": "dry-run",
                "score": float(score_value),
                "duration_ms": duration_ms,
                "text": text[:80],
            }

        # The "spx" attachment is the raw Speex payload of the recording.  It is
        # optional for the server (the official client sends an empty value for
        # every answer type that has no recording), so a missing encoder must
        # never block the submission.
        speex_path: Optional[Path] = None
        try:
            speex_path = self._audio.to_speex(raw_path)
        except audio_mod.AudioError as exc:
            # SpeexUnavailableError (no encoder at all) as well as a failing
            # encode: the answer is submitted without the spx attachment.
            self._warn_speex_once(str(exc))

        # Upload the attachments with small pauses in between.
        self.pacer.upload()
        mp3 = uploader.upload(mp3_path, "audio")
        speex_url = ""
        if speex_path is not None:
            self.pacer.upload()
            speex = uploader.upload(speex_path, "speex")
            speex_url = speex["url"]
        self.pacer.upload()
        xml_result = uploader.upload(xml_path, "xml")

        score_data = score_mod.build_score_data(
            category, float(score_value), maximum=max(segment_max, 5.0)
        )

        context = dict(question.context)
        context["order"] = str(question.order)
        context["serial_id"] = self._next_serial_id()
        # The official client reports how long the answer took (in seconds).
        # A zero here makes the server treat the answer as "not practised".
        context["use_time"] = max(1, int(round((duration_ms or 0) / 1000.0)))
        context["correct"] = 1

        self.pacer.request()
        mark_error: Optional[EtsApiError] = None
        attempts = max(1, int(self.settings.get("mark_retries", 2) or 0) + 1)
        for attempt in range(1, attempts + 1):
            self._check_cancel()
            try:
                self.client.submit_audio(
                    token=token,
                    context=context,
                    score=score_value,
                    score_detail=score_data.to_json(),
                    mp3_url=mp3["url"],
                    speex_url=speex_url,
                    xml_url=xml_result["url"],
                )
                mark_error = None
                break
            except EtsApiError as exc:
                if not _is_transient_mark_error(exc) or attempt >= attempts:
                    mark_error = exc
                    break
                # The answer was delivered, only ETS' own marking service was
                # unreachable.  Retrying the same entity/order is safe - the
                # endpoint overwrites the record instead of duplicating it.
                self._info(
                    "ETS 评分服务暂时不可用（%s），%d 秒后重试（第 %d 次）",
                    exc,
                    MARK_RETRY_DELAY_SECONDS,
                    attempt,
                )
                self._emit(
                    "warn",
                    f"ETS 评分服务暂时不可用，{MARK_RETRY_DELAY_SECONDS} 秒后重试",
                    group=question.group_name,
                )
                self._wait(MARK_RETRY_DELAY_SECONDS)

        unconfirmed = False
        if mark_error is not None:
            if not _is_transient_mark_error(mark_error):
                raise mark_error
            # The audio, the spx and the score XML are on the server; only the
            # score could not be computed.  Reporting this as a failed question
            # would be wrong, so it is reported as "submitted, not confirmed".
            unconfirmed = True
            self._warn_mark_once(str(mark_error))
            self._emit(
                "warn",
                f"{label} 已提交，但 ETS 评分服务返回错误：{mark_error}"
                "（答案已上传，可在官方 App 里确认是否记录）",
                group=question.group_name,
            )

        if not self.settings.get("keep_audio_cache", True):
            _cleanup(raw_path, mp3_path, xml_path, speex_path)
        else:
            _cleanup(raw_path)

        return {
            "index": segment_index,
            "status": "submitted-unconfirmed" if unconfirmed else "submitted",
            "score": float(score_value),
            "duration_ms": duration_ms,
            "text": text[:80],
            "mp3_url": mp3["url"],
            "speex_uploaded": bool(speex_url),
            "message": str(mark_error) if unconfirmed else "",
        }

    def _complete_choice(
        self,
        token: str,
        question: paper_mod.Question,
        per_question_max: float,
        label: str,
        dry_run: bool,
    ) -> Dict[str, Any]:
        answer = (question.correct_answer or "").strip()
        if not answer:
            return {
                "group": question.group_name,
                "order": question.order,
                "kind": question.kind,
                "status": "skipped",
                "message": "未解析到标准答案",
            }
        self.pacer.reading(question.question_text or answer)
        score_value = score_mod.clamp_score(per_question_max, per_question_max)

        if dry_run:
            return {
                "group": question.group_name,
                "order": question.order,
                "kind": question.kind,
                "status": "dry-run",
                "score": float(score_value),
                "answer": answer,
            }

        category = score_mod.category_for(question.structure_type, question.group_name)
        score_data = score_mod.build_score_data(
            category, float(score_value), choose=answer, maximum=max(per_question_max, 1.5)
        )
        context = dict(question.context)
        context["order"] = str(question.order)
        context["correct"] = 1

        self.pacer.request()
        self.client.submit_choice(
            token=token,
            context=context,
            score=score_value,
            score_detail=score_data.to_json(),
            correct=1,
            choose=answer,
        )
        return {
            "group": question.group_name,
            "order": question.order,
            "kind": question.kind,
            "status": "submitted",
            "score": float(score_value),
            "answer": answer,
        }

    # -- voice setup ---------------------------------------------------------

    def clone_voice(
        self,
        zh_sample: Path,
        en_sample: Optional[Path] = None,
        en_text: str = "",
        voice_name: str = "FastETSVoice",
    ) -> Dict[str, Any]:
        """Train/clone the user's voice from the recorded samples."""
        provider = self.provider
        if not provider.supports_clone:
            raise ProviderError(
                f"{provider.name} 不支持声音克隆，请改用 MiniMax 或配置克隆端点"
            )
        self._emit("clone", "上传样本并克隆声音", percent=10)
        result = provider.clone(
            Path(zh_sample),
            voice_name,
            prompt_path=Path(en_sample) if en_sample else None,
            prompt_text=en_text,
        )
        # Store the cloned voice under the setting the active provider reads.
        if provider.name == "openai_compat":
            self.settings["openai_voice"] = result.voice_id
        elif provider.name == "edge":
            self.settings["edge_voice"] = result.voice_id
        else:
            self.settings["minimax_voice_id"] = result.voice_id
        config.save_settings(self.settings)
        self._emit("clone", f"声音克隆完成：{result.voice_id}", percent=100)
        return {"voice_id": result.voice_id, "provider": result.provider}


def _join_url(base_url: str, path: str) -> str:
    if path.startswith(("http://", "https://")):
        return path.replace("http://", "https://", 1)
    base = base_url.replace("http://", "https://", 1).rstrip("/")
    return f"{base}/{path.lstrip('/')}"


def _cleanup(*paths: Path) -> None:
    for path in paths:
        try:
            if path and Path(path).exists():
                Path(path).unlink()
        except OSError:
            pass
