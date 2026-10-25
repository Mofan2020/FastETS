"""Parsing of the assignment papers stored in ``content.json``.

The ETS100 resource packs describe each question with a ``structure_type``.
The known families are:

===============================  ==========================================
``structure_type``               题型
===============================  ==========================================
``collector.role``               听选信息 / 回答问题 / 逐句跟读
``collector.3q5a``               广东高中 3 问 5 答
``collector.choose``             听后选择
``collector.picture``            信息转述 / 听后转述
``collector.fill``               听后记录 / 填空
``collector.dialogue``           回答问题
``collector.read``               模仿朗读 / 短文朗读
===============================  ==========================================
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Dict, List, Optional

from .. import config

# --- text helpers ------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_ETS_PREFIX_RE = re.compile(r"ets_th\d+")


def clean_text(value: Any) -> str:
    """Normalise the raw HTML-ish strings used inside ``content.json``."""
    if value is None:
        return ""
    text = str(value)
    text = _ETS_PREFIX_RE.sub("", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>\s*<p>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = text.replace("\u200b", "")
    text = html.unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sentences(text: str) -> List[str]:
    """Split a passage into sentences (used by 逐句跟读)."""
    if not text:
        return []
    parts = re.split(r"(?<=[.!?。！？])\s+", text)
    sentences = [part.strip() for part in parts if part and part.strip()]
    return sentences or [text.strip()]


def part_max_score(part_name: str) -> float:
    """Total score of one 大题, mirroring the official client."""
    if "听后选择" in part_name:
        return 1.5
    if "听后回答" in part_name:
        return 2.0
    if "听后转述" in part_name:
        return 10.0
    if "短文朗读" in part_name:
        return 9.0
    return 5.0


# --- deep lookup --------------------------------------------------------------


def find_deep(source: Any, keys: List[str]) -> Any:
    """Find the first non-empty value for any of ``keys`` anywhere in a tree."""
    if not isinstance(source, (dict, list)):
        return None
    if isinstance(source, dict):
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return value
        for value in source.values():
            found = find_deep(value, keys)
            if found is not None:
                return found
        return None
    for item in source:
        found = find_deep(item, keys)
        if found is not None:
            return found
    return None


def _make_context(
    payload: Dict[str, Any],
    homework_id: str,
    order: int,
    context_base: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "homework_id": homework_id,
        "resource_id": str(
            context_base.get("resource_id")
            or find_deep(payload, ["resource_id", "resourceId"])
            or config.ETS_EVALUATE_RESOURCE_ID
        ),
        "set_id": str(context_base.get("set_id") or find_deep(payload, ["set_id", "setId"]) or ""),
        "entity_id": str(find_deep(payload, ["entity_id", "entityId", "stid"]) or ""),
        "order": str(find_deep(payload, ["order"]) or order),
        "serial_id": str(find_deep(payload, ["serial_id", "serialId"]) or "0"),
        "use_time": _as_int(find_deep(payload, ["use_time", "useTime"]), 0),
        "score": str(find_deep(payload, ["score"]) or "0.0"),
        "correct": _as_int(find_deep(payload, ["correct"]), 1),
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# --- questions ----------------------------------------------------------------


class Question:
    """One answerable unit inside a paper."""

    def __init__(
        self,
        question_id: str,
        homework_id: str,
        content_url: str,
        order: int,
        group_name: str,
        structure_type: str,
        question_text: str,
        answers: List[str],
        context: Dict[str, Any],
        options: Optional[List[Dict[str, str]]] = None,
        correct_answer: str = "",
        original_text: str = "",
        max_score: float = 5.0,
        read_text: str = "",
        answer_candidates: Optional[List[str]] = None,
    ) -> None:
        self.id = question_id
        self.homework_id = homework_id
        self.content_url = content_url
        self.order = order
        self.group_name = group_name
        self.structure_type = structure_type
        self.question_text = question_text
        self.answers = answers
        self.context = context
        self.options = options or []
        self.correct_answer = correct_answer
        self.original_text = original_text
        self.max_score = max_score
        self.read_text = read_text
        # 复述 / 表达这类题型在资源包里带多份「示例答案」，朗读时需要从
        # 里面挑一份（第一个 / 随机 / 手动选择）。
        self.answer_candidates = [c for c in (answer_candidates or []) if c]

    # -- classification -----------------------------------------------------

    @property
    def is_choice(self) -> bool:
        return self.structure_type == "collector.choose"

    @property
    def needs_audio(self) -> bool:
        """Choice questions are answered by tapping, everything else needs audio."""
        return not self.is_choice

    @property
    def kind(self) -> str:
        """Human readable assignment family."""
        structure = self.structure_type
        name = self.group_name
        if structure == "collector.choose":
            return "听后选择"
        if structure == "collector.fill":
            return "听后记录"
        if structure == "collector.picture":
            return "信息转述"
        if structure == "collector.read":
            return "短文朗读" if "朗读" in name else "模仿朗读"
        if structure in ("collector.role", "collector.3q5a", "collector.dialogue"):
            if "跟读" in name or "模仿" in name:
                return "逐句跟读"
            if "朗读" in name:
                return "朗读"
            if "转述" in name:
                return "信息转述"
            if "提问" in name or "问答" in name:
                return "回答问题"
            return "听选信息"
        return name or "其他"

    def speech_segments(self, text: str = "") -> List[str]:
        """The text that has to be spoken, split into segments.

        The official client records one clip per sentence for 跟读 / 朗读, while
        短文朗读 and 信息转述 are submitted as a single recording.  ``text``
        overrides the parsed text, which is how 复述 / 表达 read one of the
        sample answers instead of the source material.
        """
        if self.is_choice:
            return []
        text = text or self.read_text or self.original_text or self.question_text
        if not text:
            return []
        kind = self.kind
        if kind in ("短文朗读", "模仿朗读", "信息转述"):
            # One recording for the whole passage.
            return [text]
        sentences = split_sentences(text)
        if kind in ("逐句跟读", "朗读"):
            return sentences
        # 听选信息 / 回答问题 / 听后记录: usually a single phrase.
        return sentences if len(sentences) > 1 else [text]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "homework_id": self.homework_id,
            "order": self.order,
            "group_name": self.group_name,
            "structure_type": self.structure_type,
            "kind": self.kind,
            "question_text": self.question_text,
            "answers": self.answers,
            "options": self.options,
            "correct_answer": self.correct_answer,
            "original_text": self.original_text,
            "read_text": self.read_text,
            "max_score": self.max_score,
            "needs_audio": self.needs_audio,
            "answer_candidates": self.answer_candidates,
            "context": self.context,
        }


class Section:
    """One 大题 (a group of questions sharing a resource pack)."""

    def __init__(self, group_name: str, structure_type: str, questions: List[Question]) -> None:
        self.group_name = group_name
        self.structure_type = structure_type
        self.questions = questions

    def to_dict(self) -> Dict[str, Any]:
        return {
            "group_name": self.group_name,
            "structure_type": self.structure_type,
            "questions": [q.to_dict() for q in self.questions],
        }


def _question_id(homework_id: str, content_url: str, order: int, suffix: str = "") -> str:
    raw = f"{homework_id}:{content_url}:{order}{suffix}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parse_content_json(
    payload: Dict[str, Any],
    homework_id: str,
    content_url: str,
    group_name: str,
    context_base: Optional[Dict[str, Any]] = None,
    order_offset: int = 0,
) -> Section:
    """Turn one ``content.json`` into a :class:`Section`."""
    context_base = context_base or {}
    structure_type = str(payload.get("structure_type") or "")
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    max_score = part_max_score(group_name)
    questions: List[Question] = []

    def local_order(index: int, entry: Dict[str, Any]) -> int:
        for key in ("xh", "xth", "xt_xh"):
            value = entry.get(key)
            number = _as_int(value, 0)
            if number > 0:
                return number
        return index + 1

    def make_question(
        entry: Dict[str, Any],
        index: int,
        question_text: str,
        answers: List[str],
        options: Optional[List[Dict[str, str]]] = None,
        correct_answer: str = "",
        original_text: str = "",
        read_text: str = "",
        suffix: str = "",
    ) -> Question:
        order = order_offset + local_order(index, entry)
        return Question(
            question_id=_question_id(homework_id, content_url, order, suffix),
            homework_id=homework_id,
            content_url=content_url,
            order=order,
            group_name=group_name,
            structure_type=structure_type,
            question_text=question_text,
            answers=answers,
            context=_make_context(payload, homework_id, order, context_base),
            options=options,
            correct_answer=correct_answer,
            original_text=original_text,
            max_score=max_score,
            read_text=read_text,
        )

    if structure_type == "collector.choose":
        entries = info.get("xtlist") or []
        for index, raw in enumerate(entries):
            entry = raw if isinstance(raw, dict) else {}
            options = []
            for option in entry.get("xxlist") or []:
                item = option if isinstance(option, dict) else {}
                options.append(
                    {
                        "label": str(item.get("xx_mc", "")),
                        "text": clean_text(item.get("xx_nr")),
                    }
                )
            answer = clean_text(entry.get("answer"))
            questions.append(
                make_question(
                    entry,
                    index,
                    clean_text(entry.get("xt_nr")),
                    [answer] if answer else [],
                    options=options,
                    correct_answer=answer,
                    original_text="\n".join(
                        f"{opt['label']}. {opt['text']}" for opt in options
                    ),
                    suffix=":choose",
                )
            )
        return Section(group_name, structure_type, questions)

    if structure_type == "collector.fill":
        entries = info.get("std") or []
        for index, raw in enumerate(entries):
            entry = raw if isinstance(raw, dict) else {}
            order = order_offset + local_order(index, entry)
            answer = clean_text(entry.get("value"))
            questions.append(
                Question(
                    question_id=_question_id(homework_id, content_url, order, ":fill"),
                    homework_id=homework_id,
                    content_url=content_url,
                    order=order,
                    group_name=group_name,
                    structure_type=structure_type,
                    question_text=clean_text(entry.get("xth")) or f"第 {order} 题",
                    answers=[answer] if answer else [],
                    context=_make_context(payload, homework_id, order, context_base),
                    max_score=max_score,
                    read_text=answer,
                )
            )
        return Section(group_name, structure_type, questions)

    if structure_type in ("collector.role", "collector.3q5a", "collector.dialogue"):
        entries = info.get("question") or []
        for index, raw in enumerate(entries):
            entry = raw if isinstance(raw, dict) else {}
            order = local_order(index, entry)
            answers = []
            for item in entry.get("std") or []:
                value = clean_text(item.get("value") if isinstance(item, dict) else item)
                if value:
                    answers.append(value)
            question_text = clean_text(
                entry.get("ask") or entry.get("question") or entry.get("topic")
            )
            original = clean_text(entry.get("value"))
            read_text = question_text or (answers[0] if answers else "") or original
            questions.append(
                Question(
                    question_id=_question_id(homework_id, content_url, order),
                    homework_id=homework_id,
                    content_url=content_url,
                    order=order_offset + order,
                    group_name=group_name,
                    structure_type=structure_type,
                    question_text=question_text,
                    answers=answers,
                    context=_make_context(payload, homework_id, order, context_base),
                    original_text=original,
                    max_score=max_score,
                    read_text=read_text,
                )
            )
        return Section(group_name, structure_type, questions)

    if structure_type == "collector.picture":
        entries = info.get("std") or []
        answers = []
        for raw in entries:
            item = raw if isinstance(raw, dict) else {}
            value = clean_text(item.get("value"))
            if value:
                answers.append(value)
        order = order_offset + 1
        read_text = clean_text(info.get("value")) or clean_text(info.get("topic"))
        questions.append(
            Question(
                question_id=_question_id(homework_id, content_url, order, ":picture"),
                homework_id=homework_id,
                content_url=content_url,
                order=order,
                group_name=group_name,
                structure_type=structure_type,
                question_text=clean_text(info.get("topic")) or group_name,
                answers=answers,
                answer_candidates=list(answers),
                context=_make_context(payload, homework_id, order, context_base),
                original_text=read_text,
                max_score=max_score,
                read_text=read_text,
            )
        )
        return Section(group_name, structure_type, questions)

    # Fallback: 模仿朗读 / 短文朗读 and anything unknown.
    read_text = clean_text(info.get("value")) or clean_text(info.get("std"))
    order = order_offset + 1
    questions.append(
        Question(
            question_id=_question_id(homework_id, content_url, order, ":read"),
            homework_id=homework_id,
            content_url=content_url,
            order=order,
            group_name=group_name,
            structure_type=structure_type,
            question_text=group_name,
            answers=[],
            context=_make_context(payload, homework_id, order, context_base),
            original_text=read_text,
            max_score=max_score,
            read_text=read_text,
        )
    )
    return Section(group_name, structure_type, questions)
