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

Structures that are not in this list are handled by a generic splitter: if
``info`` carries a list of items the pack becomes one question per item
instead of a single question for the whole pack.  See :func:`_find_item_entries`.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Dict, List, Optional

from .. import config

# The structure types that are parsed explicitly.  Everything else goes
# through the generic item splitter.
KNOWN_STRUCTURE_TYPES = frozenset(
    {
        "collector.choose",
        "collector.fill",
        "collector.role",
        "collector.3q5a",
        "collector.dialogue",
        "collector.picture",
        "collector.read",
        "collector.word",
        "collector.repeat_essay",
        "collector.repeat_dialogue",
    }
)

# ``info`` keys that hold a list of questions / items.
_ITEM_LIST_KEYS = (
    "question",
    "sublist",
    "exam_info",
    "items",
    "list",
    "word",
    "words",
)
# Keys that may hold a list of items, but only when the entries carry their
# own number - there they can also be the alternative answers of one question.
_WEAK_ITEM_LIST_KEYS = ("std", "xtlist")
# Keys that carry the number of an item.
_ITEM_NUMBER_KEYS = ("seq", "xh", "xth", "xt_xh", "num", "no", "index")
# Keys that carry the text of an item.
_ITEM_TEXT_KEYS = (
    "value",
    "text",
    "content",
    "topic",
    "ask",
    "question",
    "word",
)
# Keys that carry the answers of an item.
_ITEM_ANSWER_KEYS = ("std", "answer", "answers")

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
    if "短文跟读" in part_name or "短文复述" in part_name:
        # The official app shows 100 for this 大题 (the sentences of the
        # passage are graded one by one, ten points each).
        return 100.0
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


def is_known_structure(structure_type: str) -> bool:
    """Whether the structure type is parsed explicitly."""
    return str(structure_type or "") in KNOWN_STRUCTURE_TYPES


# --- generic item splitting ----------------------------------------------------


def _entry_number(entry: Dict[str, Any], index: int) -> int:
    """The number of an item (``seq`` / ``xh`` / ``xth`` ...)."""
    for key in _ITEM_NUMBER_KEYS:
        number = _as_int(entry.get(key), 0)
        if number > 0:
            return number
    return index + 1


def _entry_text(entry: Dict[str, Any]) -> str:
    """The readable text of an item."""
    for key in _ITEM_TEXT_KEYS:
        text = clean_text(entry.get(key))
        if text:
            return text
    return ""


def _entry_answers(entry: Dict[str, Any]) -> List[str]:
    """The answers of an item (a list, they are alternatives)."""
    raw: Any = None
    for key in _ITEM_ANSWER_KEYS:
        if entry.get(key):
            raw = entry.get(key)
            break
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        value = clean_text(raw)
        return [value] if value else []
    if not isinstance(raw, list):
        return []
    answers: List[str] = []
    for item in raw:
        value = clean_text(item.get("value") if isinstance(item, dict) else item)
        if value:
            answers.append(value)
    return answers


def _find_item_entries(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The list of items inside ``info``, if the pack has one.

    The strong keys (``sublist`` and friends) always describe a list of
    items.  The weak keys (``std`` / ``xtlist``) are only used when the
    entries carry their own number, because there they can also be the
    alternative answers of a single question (``collector.picture``).
    """
    for key in _ITEM_LIST_KEYS:
        raw = info.get(key)
        if not isinstance(raw, list) or not raw:
            continue
        entries = [item for item in raw if isinstance(item, dict)]
        if entries:
            return entries
    for key in _WEAK_ITEM_LIST_KEYS:
        raw = info.get(key)
        if not isinstance(raw, list) or not raw:
            continue
        entries = [item for item in raw if isinstance(item, dict)]
        if not entries:
            continue
        if any(any(name in entry for name in _ITEM_NUMBER_KEYS) for entry in entries):
            return entries
    return []


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
        single_recording: bool = False,
        pack_order: int = 1,
        sentences: Optional[List[str]] = None,
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
        # 复述 / 表达 / 交际应答这类题型在资源包里带多份「示例答案」，朗读时
        # 需要从里面挑一份（第一个 / 随机 / 手动选择）。
        self.answer_candidates = [c for c in (answer_candidates or []) if c]
        # One question = one recording.  Used by the generic splitter, where
        # the shape of the pack is unknown and the text must not be split
        # into sentences (that would create several records for one
        # question and the completion would never reach 100%).
        self.single_recording = bool(single_recording)
        # The position of the question **inside its own resource pack** (1
        # based).  The server identifies an answer by "pack + item number",
        # and the item number restarts with every pack - see
        # ``docs/API.md``.
        self.pack_order = int(pack_order or 1)
        # The sentence list the server knows about (短文跟读 ships one entry
        # per sentence with its timing).  The score XML has to use exactly
        # this split, otherwise the marking service cannot map its result
        # back to the sentences.
        self.sentences = [s for s in (sentences or []) if s]

    # -- classification -----------------------------------------------------

    @property
    def is_choice(self) -> bool:
        return self.structure_type == "collector.choose"

    @property
    def is_response(self) -> bool:
        """交际应答 / 快速应答: one answer out of many, one recording."""
        return (
            self.structure_type == "collector.dialogue"
            or "交际应答" in self.group_name
            or "快速应答" in self.group_name
        )

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
        if kind in ("短文朗读", "模仿朗读", "信息转述") or self.is_response:
            # One recording for the whole passage / the whole answer.
            return [text]
        if self.single_recording:
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
            "single_recording": self.single_recording,
            "sentences": self.sentences,
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
        local = local_order(index, entry)
        order = order_offset + local
        return Question(
            question_id=_question_id(homework_id, content_url, order, suffix),
            homework_id=homework_id,
            content_url=content_url,
            order=order,
            pack_order=local,
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
            local = local_order(index, entry)
            order = order_offset + local
            answer = clean_text(entry.get("value"))
            questions.append(
                Question(
                    question_id=_question_id(homework_id, content_url, order, ":fill"),
                    homework_id=homework_id,
                    content_url=content_url,
                    order=order,
                    pack_order=local,
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
        # 交际应答 / 快速应答: the "std" entries are alternative answers to the
        # situation, so only one of them is spoken - never all of them.
        is_response = (
            structure_type == "collector.dialogue"
            or "交际应答" in group_name
            or "快速应答" in group_name
        )
        for index, raw in enumerate(entries):
            entry = raw if isinstance(raw, dict) else {}
            local = local_order(index, entry)
            order = local
            answers = []
            for item in entry.get("std") or []:
                value = clean_text(item.get("value") if isinstance(item, dict) else item)
                if value:
                    answers.append(value)
            question_text = clean_text(
                entry.get("ask") or entry.get("question") or entry.get("topic")
            )
            original = clean_text(entry.get("value"))
            if is_response and answers:
                read_text = answers[0]
            else:
                read_text = question_text or (answers[0] if answers else "") or original
            questions.append(
                Question(
                    question_id=_question_id(homework_id, content_url, order),
                    homework_id=homework_id,
                    content_url=content_url,
                    order=order_offset + order,
                    pack_order=local,
                    group_name=group_name,
                    structure_type=structure_type,
                    question_text=question_text,
                    answers=answers,
                    context=_make_context(payload, homework_id, order, context_base),
                    original_text=original,
                    max_score=max_score,
                    read_text=read_text,
                    answer_candidates=list(answers) if is_response else None,
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
                pack_order=1,
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

    if structure_type == "collector.word":
        # One word per pack: the student reads the word itself.
        order = order_offset + 1
        word = clean_text(info.get("value"))
        questions.append(
            Question(
                question_id=_question_id(homework_id, content_url, order, ":word"),
                homework_id=homework_id,
                content_url=content_url,
                order=order,
                pack_order=1,
                group_name=group_name,
                structure_type=structure_type,
                question_text=clean_text(info.get("translate")) or group_name,
                answers=[word] if word else [],
                context=_make_context(payload, homework_id, order, context_base),
                original_text=word,
                max_score=max_score,
                read_text=word,
                single_recording=True,
            )
        )
        return Section(group_name, structure_type, questions)

    if structure_type in ("collector.repeat_essay", "collector.repeat_dialogue"):
        # 短文跟读 / 短文复述: ONE passage, ONE recording.
        #
        # The pack carries the whole passage in ``info.value`` and its
        # sentence-by-sentence breakdown in ``info.sublist`` (each entry with
        # ``seq``, ``text`` and the ``begintime`` / ``endtime`` of that
        # sentence inside the original audio).  That timeline is what the
        # server uses to align the student's recording, so the pack is a
        # single item with a single continuous clip - not one item per
        # sentence.  Splitting it made the server keep a single sentence and
        # score the 大题 12 out of 100.
        sentences: List[str] = []
        for raw in info.get("sublist") or []:
            entry = raw if isinstance(raw, dict) else {}
            text = clean_text(entry.get("text") if entry else raw)
            if text:
                sentences.append(text)
        passage = clean_text(info.get("value")) or " ".join(sentences)
        order = order_offset + 1
        questions.append(
            Question(
                question_id=_question_id(homework_id, content_url, order, ":essay"),
                homework_id=homework_id,
                content_url=content_url,
                order=order,
                pack_order=1,
                group_name=group_name,
                structure_type=structure_type,
                question_text="朗读下列短文。",
                # The passage has no "answers" - the sentences are kept in
                # ``sentences`` so the preview shows the passage itself
                # instead of a list joined with slashes.
                answers=[],
                context=_make_context(payload, homework_id, order, context_base),
                original_text=passage,
                max_score=max_score,
                read_text=passage,
                single_recording=True,
                sentences=sentences,
            )
        )
        return Section(group_name, structure_type, questions)

    # An unknown structure type: if the pack carries a list of items, split it
    # into one question per item.  Without this the whole pack would be
    # recorded as a single answer and the assignment could never reach 100%.
    entries = (
        _find_item_entries(info) if not is_known_structure(structure_type) else []
    )
    if entries:
        for index, entry in enumerate(entries):
            number = _entry_number(entry, index)
            order = order_offset + number
            answers = _entry_answers(entry)
            text = _entry_text(entry)
            prompt = clean_text(
                entry.get("ask") or entry.get("question") or entry.get("topic")
            )
            questions.append(
                Question(
                    question_id=_question_id(homework_id, content_url, order, ":item"),
                    homework_id=homework_id,
                    content_url=content_url,
                    order=order,
                    pack_order=number,
                    group_name=group_name,
                    structure_type=structure_type,
                    question_text=prompt or text,
                    answers=answers,
                    context=_make_context(payload, homework_id, order, context_base),
                    original_text=text,
                    max_score=max_score,
                    # The answers are alternatives, so one of them is read.
                    read_text=answers[0] if answers else text,
                    answer_candidates=list(answers) if len(answers) > 1 else None,
                    single_recording=True,
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
            pack_order=1,
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
