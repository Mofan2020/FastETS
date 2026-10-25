"""Score documents in the iFlytek evaluation XML format.

The ETS100 server stores a *score detail* document alongside the recording.
FastETS writes the same three document shapes the official client produces:

``simple_expression``  跟读 / 回答问题 / 选择题
``topic``              信息转述
``read_chapter``       短文朗读 / 模仿朗读
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

CLIENT_VERSION = "7.0.71.2569(2569)"
INTERNAL_FULL_SCORE = 5.0


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
    if structure_type == "collector.picture" or "转述" in group_name:
        return "topic"
    if structure_type == "collector.read" or "朗读" in group_name:
        return "read_chapter"
    return "simple_expression"


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
) -> str:
    """Build the evaluation detail XML for one submission."""
    content = (text or "").strip() or "full score"
    internal = float(score if internal_score is None else internal_score)

    if category == "topic":
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

    if category == "read_chapter":
        sentence_nodes: List[str] = []
        for sentence_index, sentence in enumerate(sentences_of(content)):
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
            f'  <read_chapter lan="en" type="study" version="V4.2.0R6.62.46.9M4.62.46.9">\n'
            f"    <rec_paper>\n"
            f'      <read_chapter accuracy_score="{internal:.6f}" beg_pos="0" '
            f'content="{escape_xml(content)}" continus_deduct_score="0.000000" '
            f'end_pos="{len(content)}" except_info="0" fluency_score="{internal:.6f}" '
            f'improper_cnt="0.000000" integrity_score="{internal:.6f}" '
            f'is_rejected="false" reject_type="0" rising_tone_deduct_score="0.000000" '
            f'score_pattern="strict" skill_deduct_score="0.000000" '
            f'standard_score="{internal:.6f}" stress_deduct_score="0.000000" '
            f'total_score="{internal:.6f}" word_count="{len(words_of(content))}" '
            f'words_per_min="120.000000">\n'
            f"{chr(10).join(sentence_nodes)}\n"
            f"      </read_chapter>\n"
            f"    </rec_paper>\n"
            f"  </read_chapter>\n"
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


def build_score_data(
    category: str,
    score: float,
    choose: str = "",
    maximum: float = 5.0,
) -> ScoreData:
    """Build the ``score_detail`` payload sent alongside the recording."""
    value = clamp_score(score, maximum)
    return ScoreData(
        total_score=value,
        choose=choose,
        fluency_score=value,
        accuracy_score=value,
        integrity_score=value,
        standard_score=value,
        category=category,
        repeat_mode="0",
        version=CLIENT_VERSION,
        rate_scale=0,
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
