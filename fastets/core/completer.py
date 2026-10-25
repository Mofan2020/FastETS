"""End-to-end automation of one assignment.

The flow per assignment is:

1. download the resource pack of every 大题 from the CDN
2. decrypt it and parse ``content.json``
3. for every question: synthesise the required speech, encode it, upload it
   to OSS and report the result back to the server
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

from .. import config
from ..voice import ProviderError, VoiceProvider, build_provider
from . import audio as audio_mod
from . import crypto, oss, paper as paper_mod, score as score_mod, zipcrypto
from .ets_api import CODE_TOKEN_EXPIRED, EtsApiClient, EtsApiError
from .pacing import Pacer, pacer_from_settings
from .session import Session

ProgressCallback = Callable[[Dict[str, Any]], None]


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
    ) -> None:
        self.session = session
        self.settings = settings if settings is not None else config.load_settings()
        self._log = logger
        self._progress_cb = progress
        self.pacer: Pacer = pacer_from_settings(self.settings)
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

    def _check_cancel(self) -> None:
        if self.pacer.stopped:
            raise CancelledError("任务已被用户取消")

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
                context_base={"set_id": set_id},
                order_offset=offset,
            )
            sections.append(section)
            offsets[group_name] = offset + len(section.questions)

        return LoadedPaper(homework, sections)

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
            "items": [],
        }

        completed = 0
        for section_index, section in enumerate(paper.sections):
            self._check_cancel()
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
            + (f"，失败 {report['failed']} 题" if report["failed"] else ""),
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

        segments = question.speech_segments()
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
        return {
            "group": question.group_name,
            "order": question.order,
            "kind": question.kind,
            "status": "submitted",
            "segments": results,
            "score": sum(item.get("score", 0.0) for item in results),
        }

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
        result = self.provider.synthesize(
            text,
            voice_id=str(self.settings.get("minimax_voice_id") or ""),
            speed=float(self.settings.get("tts_speed", 1.0) or 1.0),
            pitch=int(self.settings.get("tts_pitch", 0) or 0),
        )

        raw_path = config.audio_cache_dir() / f"{crypto.random_hex(16)}.raw"
        raw_path.write_bytes(result.audio)

        mp3_path = self._audio.to_mp3(raw_path)
        speex_path = self._audio.to_speex(raw_path)
        duration_ms = result.duration_ms or self._audio.duration_ms(mp3_path)

        category = score_mod.category_for(question.structure_type, question.group_name)
        target_score = self._score_for(question, segment_max, text, duration_ms)
        score_value = score_mod.clamp_score(target_score, segment_max)
        xml_text = score_mod.build_score_xml(text, category, target_score)
        xml_path = config.audio_cache_dir() / f"{crypto.random_hex(16)}.xml"
        xml_path.write_text(xml_text, encoding="utf-8")

        if dry_run:
            if not self.settings.get("keep_audio_cache", True):
                _cleanup(raw_path, mp3_path, speex_path, xml_path)
            return {
                "index": segment_index,
                "status": "dry-run",
                "score": float(score_value),
                "duration_ms": duration_ms,
                "text": text[:80],
            }

        # Upload the three attachments with small pauses in between.
        self.pacer.upload()
        mp3 = uploader.upload(mp3_path, "audio")
        self.pacer.upload()
        speex = uploader.upload(speex_path, "speex")
        self.pacer.upload()
        xml_result = uploader.upload(xml_path, "xml")

        score_data = score_mod.build_score_data(
            category, float(score_value), maximum=max(segment_max, 5.0)
        )

        context = dict(question.context)
        context["order"] = str(question.order)
        context["serial_id"] = str(segment_index)
        context["use_time"] = duration_ms
        context["correct"] = 1

        self.pacer.request()
        self.client.submit_audio(
            token=token,
            context=context,
            score=score_value,
            score_detail=score_data.to_json(),
            mp3_url=mp3["url"],
            speex_url=speex["url"],
            xml_url=xml_result["url"],
        )

        if not self.settings.get("keep_audio_cache", True):
            _cleanup(raw_path, mp3_path, speex_path, xml_path)
        else:
            _cleanup(raw_path)

        return {
            "index": segment_index,
            "status": "submitted",
            "score": float(score_value),
            "duration_ms": duration_ms,
            "text": text[:80],
            "mp3_url": mp3["url"],
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
