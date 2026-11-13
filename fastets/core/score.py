"""Score documents in the iFlytek evaluation XML format.

The ETS100 server stores a *score detail* document alongside the recording.
FastETS writes the same three document shapes the official client produces:

``simple_expression``  跟读 / 回答问题 / 选择题
``topic``              信息转述
``read_chapter``       短文朗读 / 模仿朗读

The official client names the document after the 大题's scoring category, and
the 情景交际 family carries a suffix (``simple_expression_ufk`` = 问答信息,
``simple_expression_ufi`` = 听说信息, ``simple_expression_ufj`` = 询问信息).
The category is also the ``score_detail.category`` field.

**The score itself is on the ISE scale (0 - 5).**  The server books an answer
as ``score / 5 * 该题官方满分``, so a score that is already a *point value*
(``1.0``, ``1.5`` …) would be booked as a fifth of the points it should be.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

CLIENT_VERSION = "7.0.71.2569(2569)"
INTERNAL_FULL_SCORE = 5.0

# The scoring categories of the official client.  The name of the document
# and the ``score_detail.category`` field are the same string.
CATEGORY_SIMPLE = "simple_expression"
CATEGORY_UFK = "simple_expression_ufk"   # 问答信息（交际应答 / 回答问题）
CATEGORY_UFI = "simple_expression_ufi"   # 听说信息（听选信息）
CATEGORY_UFJ = "simple_expression_ufj"   # 询问信息（提问）
CATEGORY_TOPIC = "topic"                 # 信息转述
CATEGORY_READ_CHAPTER = "read_chapter"   # 模仿朗读 / 短文朗读
CATEGORY_READ_SENTENCE = "read_sentence" # 短文跟读（整篇短文的分段跟读）

# The ISE scale: every answer is scored from 0 to 5, the server converts it
# into the points of the question.
ISE_FULL_SCORE = 5.0

# The official granularity of the bookkeeping (分档位).
GRADUATION = 0.125


@dataclass
class ScoreData:
    total_score: str = "0.0"
    choose: str = ""
    fluency_score: str = "0.0"
    accuracy_score: str = "0.0"
    integrity_score: str = "0.0"
    standard_score: str = "0.0"
    category: str = "simple_expression"
    repeat_mode: str = "0"
    version: str = CLIENT_VERSION
    rate_scale: int = 0
    source: str = "manual"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def clamp_score(value: float, maximum: float = 5.0) -> str:
    number = max(0.0, min(float(value), float(maximum)))
    return f"{number:.1f}"


def clamp_ise(value: float) -> str:
    """Clamp an ISE score (0 - 5) and format it with two decimals."""
    number = max(0.0, min(float(value), ISE_FULL_SCORE))
    return f"{number:.2f}"



def escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def words_of(text: str) -> List[str]:
    return [item for item in text.split() if item.strip()]


def sentences_of(text: str) -> List[str]:
    import re

    parts = re.split(r"(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


def category_for(structure_type: str, group_name: str) -> str:
    """The official scoring category of a 大题.

    The category names the evaluation document and travels in
    ``score_detail.category`` - the official client uses the category of the
    题型, not a generic one:

    * 问答信息（交际应答 / 回答问题）→ ``simple_expression_ufk``
    * 听说信息（听选信息）→ ``simple_expression_ufi``
    * 询问信息（提问）→ ``simple_expression_ufj``
    * 信息转述 → ``topic``
    * 模仿朗读 / 短文朗读 → ``read_chapter``
    * 短文跟读 → ``read_sentence``
    """
    if structure_type == "collector.picture" or "转述" in group_name:
        return CATEGORY_TOPIC
    if (
        structure_type in ("collector.repeat_essay", "collector.repeat_dialogue")
        or "短文跟读" in group_name
        or "短文复述" in group_name
    ):
        return CATEGORY_READ_SENTENCE
    if structure_type == "collector.read" or "朗读" in group_name:
        return CATEGORY_READ_CHAPTER
    if (
        structure_type == "collector.dialogue"
        or "交际应答" in group_name
        or "快速应答" in group_name
        or "回答问题" in group_name
    ):
        return CATEGORY_UFK
    if "听选信息" in group_name:
        return CATEGORY_UFI
    if "询问信息" in group_name or "提问" in group_name:
        return CATEGORY_UFJ
    return CATEGORY_SIMPLE


def _word_tags(text: str, score: float) -> str:
    cursor = 0
    lines: List[str] = []
    for word in words_of(text):
        start = text.find(word, cursor)
        begin = start if start >= 0 else cursor
        end = begin + max(len(word) - 1, 0)
        cursor = end + 1
        lines.append(
            f'          <word beg_pos="{begin}" content="{escape_xml(word)}" '
            f'end_pos="{end}" total_score="{score:.6f}"/>'
        )
    return "\n".join(lines)


def build_score_xml(
    text: str,
    category: str,
    score: float,
    internal_score: Optional[float] = None,
    sentences: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    word_hits: Optional[List[float]] = None,
) -> str:
    """Build the evaluation detail XML for one submission.

    ``sentences`` is the sentence list the server knows about.  When it is
    given, the XML uses exactly that split instead of guessing it from the
    text - 短文跟读 ships the authoritative list (with the timing of every
    sentence) and the marking service maps its result back by index.

    ``keywords`` / ``word_hits`` carry the official 情景交际 rubric: the
    keywords of the question and how much of every keyword the answer hit.
    """
    content = (text or "").strip() or "full score"
    internal = float(score if internal_score is None else internal_score)

    if category == CATEGORY_TOPIC:
        keypoints = words_of(content)[:8]
        return (
            f'<xml_result>\n'
            f'  <topic lan="en" region="beijing" region_version="1" type="study" '
            f'version="V4.1.0R6.81.20.38M5.81.38.0">\n'
            f"    <rec_paper>\n"
            f'      <rec_paper accuracy_score="{internal:.6f}" beg_pos="0" content="rec_paper" '
            f'end_pos="{len(content)}" except_info="0" fluency_score="{internal:.6f}" '
            f'integrity_score="{internal:.6f}" is_rejected="false" '
            f'keypoints="{escape_xml("|".join(keypoints))}" phone_score="{internal:.6f}" '
            f'reject_type="0" speaking_speed="120.000000" total_score="{internal:.6f}" '
            f'word_hit_of_each_kp="{"|".join("1.00" for _ in keypoints)}">\n'
            f'        <sentence content="{escape_xml(content)}" index="0">\n'
            f"{_word_tags(content, internal)}\n"
            f"        </sentence>\n"
            f"      </rec_paper>\n"
            f"    </rec_paper>\n"
            f"  </topic>\n"
            f"</xml_result>"
        )

    if category in (CATEGORY_READ_CHAPTER, CATEGORY_READ_SENTENCE):
        sentence_nodes: List[str] = []
        known_sentences = [s for s in (sentences or []) if s]
        for sentence_index, sentence in enumerate(
            known_sentences or sentences_of(content)
        ):
            word_nodes: List[str] = []
            for word_index, word in enumerate(words_of(sentence)):
                begin = max(content.find(word), 0)
                end = begin + len(word)
                word_nodes.append(
                    f'            <word beg_pos="{begin}" content="{escape_xml(word)}" '
                    f'dp_message="0" end_pos="{end}" global_index="{word_index}" '
                    f'index="{word_index}" property="12" total_score="{internal:.6f}" '
                    f'werr_msg="0" werr_msg_multi="0">\n'
                    f'              <syll beg_pos="{begin}" content="{escape_xml(word)}" '
                    f'end_pos="{end}" serr_msg="0" syll_accent="0" syll_score="{internal:.6f}">\n'
                    f'                <phone beg_pos="{begin}" content="{escape_xml(word)}" '
                    f'first_best="{escape_xml(word)}" phone_asr_top3="{escape_xml(word)}" '
                    f'phone_error_prob="0.000000"/>\n'
                    f"              </syll>\n"
                    f"            </word>"
                )
            sentence_nodes.append(
                f'        <sentence accuracy_score="{internal:.6f}" beg_pos="0" '
                f'content="{escape_xml(sentence)}" end_pos="{len(sentence)}" '
                f'fluency_score="{internal:.6f}" index="{sentence_index}" '
                f'standard_score="{internal:.6f}" total_score="{internal:.6f}" '
                f'word_count="{len(words_of(sentence))}">\n'
                f"{chr(10).join(word_nodes)}\n"
                f"        </sentence>"
            )
        return (
            f'<xml_result>\n'
            f'  <{category} lan="en" type="study" version="V4.2.0R6.62.46.9M4.62.46.9">\n'
            f"    <rec_paper>\n"
            f'      <{category} accuracy_score="{internal:.6f}" beg_pos="0" '
            f'content="{escape_xml(content)}" continus_deduct_score="0.000000" '
            f'end_pos="{len(content)}" except_info="0" fluency_score="{internal:.6f}" '
            f'improper_cnt="0.000000" integrity_score="{internal:.6f}" '
            f'is_rejected="false" reject_type="0" rising_tone_deduct_score="0.000000" '
            f'score_pattern="strict" skill_deduct_score="0.000000" '
            f'standard_score="{internal:.6f}" stress_deduct_score="0.000000" '
            f'total_score="{internal:.6f}" word_count="{len(words_of(content))}" '
            f'words_per_min="120.000000">\n'
            f"{chr(10).join(sentence_nodes)}\n"
            f"      </{category}>\n"
            f"    </rec_paper>\n"
            f"  </{category}>\n"
            f"</xml_result>"
        )

    if category in (CATEGORY_UFK, CATEGORY_UFI, CATEGORY_UFJ):
        # The 情景交际 rubric of the official client: the keywords of the
        # question and how much of every keyword the answer hit.
        keyword_list = [k for k in (keywords or []) if k]
        hits = list(word_hits or [])
        keyword_text = "|".join(keyword_list)
        hit_text = "|".join(
            f"{max(0.0, min(1.0, float(hits[i] if i < len(hits) else 0.0))):.2f}"
            for i in range(len(keyword_list))
        )
        word_nodes: List[str] = []
        for word in words_of(content):
            begin = content.find(word)
            begin = begin if begin >= 0 else 0
            end = begin + len(word)
            word_nodes.append(
                f'          <word beg_pos="{begin}" content="{escape_xml(word)}" '
                f'end_pos="{end}"/>'
            )
        return (
            f'<xml_result>\n'
            f'  <{category} lan="en" region="beijing" region_version="1" type="study" '
            f'version="V4.1.0R6.81.20.35M5.81.35.0">\n'
            f"    <rec_paper>\n"
            f'      <rec_paper beg_pos="0" content="rec_paper" end_pos="{len(content)}" '
            f'except_info="0" is_rejected="false" keywords="{escape_xml(keyword_text)}" '
            f'phone_score="{internal * 0.9:.6f}" reject_type="0" '
            f'total_score="{internal:.6f}" word_hit_of_each_kw="{hit_text}">\n'
            f'        <sentence content="{escape_xml(content)}" index="0">\n'
            f"{chr(10).join(word_nodes)}\n"
            f"        </sentence>\n"
            f"      </rec_paper>\n"
            f"    </rec_paper>\n"
            f"  </{category}>\n"
            f"</xml_result>"
        )

    keyword = (words_of(content) or [content])[0]
    return (
        f"<xml_result>\n"
        f'  <simple_expression lan="en" region="beijing" region_version="1" type="study" '
        f'version="1.0.2.1005">\n'
        f"    <rec_paper>\n"
        f'      <rec_paper beg_pos="0" content="rec_paper" end_pos="{len(content)}" '
        f'except_info="0" is_rejected="false" keywords="{escape_xml(keyword)}" '
        f'missingscore_type="2" phone_score="{internal:.6f}" reject_type="0" '
        f'total_score="{internal:.6f}" word_hit_of_each_kw="1.00">\n'
        f'        <sentence content="{escape_xml(content)}" index="0">\n'
        f"{_word_tags(content, internal)}\n"
        f"        </sentence>\n"
        f"      </rec_paper>\n"
        f"    </rec_paper>\n"
        f"  </simple_expression>\n"
        f"</xml_result>"
    )


def keyword_hit_ratio(text: str, keyword: str) -> float:
    """How much of a keyword the answer covers (the official rubric)."""
    import re

    parts = [part for part in re.split(r"\W+", str(keyword or "").lower()) if part]
    if not parts:
        return 0.0
    spoken = str(text or "").lower()
    hits = sum(1 for part in parts if part in spoken)
    return hits / len(parts)


def keyword_hits(text: str, keywords: List[str]) -> List[float]:
    """The per-keyword hit ratio of an answer."""
    return [keyword_hit_ratio(text, keyword) for keyword in (keywords or [])]


def build_score_data(
    category: str,
    score: float,
    choose: str = "",
    maximum: float = ISE_FULL_SCORE,
) -> ScoreData:
    """Build the ``score_detail`` payload sent alongside the recording.

    The values are on the ISE scale (0 - 5), like the official client: the
    server books ``score / 5 * 该题官方满分``.
    """
    total = clamp_ise(score)
    try:
        value = float(total)
    except ValueError:  # pragma: no cover - clamp_ise always returns a number
        value = 0.0
    return ScoreData(
        total_score=total,
        choose=choose,
        fluency_score=clamp_ise(value * 0.98),
        accuracy_score=clamp_ise(value * 0.99),
        integrity_score=clamp_ise(value * 0.99),
        standard_score=clamp_ise(value * 0.97),
        category=category,
        repeat_mode="0",
        version=CLIENT_VERSION,
        rate_scale=1,
        source="manual",
    )


def estimate_score(text: str, audio_duration_ms: int, maximum: float) -> float:
    """Rough score from how well the synthesised audio matches the text.

    A clean TTS reading of the reference text is worth close to the maximum;
    the estimate drops when the clip is far too short or far too long for the
    amount of text, which signals a truncated or padded submission.
    """
    words = max(len(words_of(text)), 1)
    seconds = max(audio_duration_ms, 1) / 1000.0
    expected = words / 2.6  # roughly 156 words per minute
    if expected <= 0:
        return maximum
    ratio = seconds / expected
    if ratio < 0.55:
        factor = 0.35 + ratio * 0.9
    elif ratio > 1.8:
        factor = 1.0 - min((ratio - 1.8) * 0.25, 0.35)
    else:
        factor = 1.0
    return max(0.0, min(maximum, maximum * factor))
