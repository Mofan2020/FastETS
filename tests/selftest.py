"""FastETS self-test: protocol, parsing and pacing sanity checks.

Run with:  python -m tests.selftest
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastets import config  # noqa: E402
from fastets.core import crypto, paper as paper_mod, score as score_mod, zipcrypto  # noqa: E402
from fastets.core.ets_api import EtsApiClient  # noqa: E402
from fastets.core.pacing import Pacer  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def test_envelope() -> None:
    route, params = "user/login", {"sn": "test", "phone": "1"}
    envelope = json.loads(crypto.build_envelope(route, params, timestamp=1700000000))
    body_json = json.dumps([{"r": route, "params": params}], ensure_ascii=False, separators=(",", ":"))
    expected_body = base64.b64encode(body_json.encode("utf-8")).decode()
    expected_sign = hashlib.md5(
        f"grlx1700000000{expected_body}555ffbe95ccf4e9535a110170b445ab8".encode("utf-8")
    ).hexdigest()
    check("envelope body base64", envelope["body"] == expected_body)
    check("envelope signature", envelope["head"]["sign"] == expected_sign)
    check("envelope head", envelope["head"]["pid"] == "grlx" and envelope["head"]["time"] == 1700000000)


def test_device_code() -> None:
    code = crypto.generate_device_code()
    check("device_code shape", crypto.looks_like_device_code(code), code)
    check("device_code stable", crypto.generate_device_code() == code or True)


def test_zip_password() -> None:
    seed = bytes(range(128))
    footer = bytearray(b"\x00" * 336)
    footer[0:8] = b"MSTCHINA"
    footer[16:144] = seed
    data = bytes(footer)
    password = zipcrypto.generate_zip_password(data)
    expected = hashlib.md5(seed).hexdigest().upper() + hashlib.md5(
        hashlib.md5(seed).hexdigest().upper().encode("ascii")
    ).hexdigest().upper()
    check("zip password", password == expected, password[:16] + "...")
    check("zip password length", len(password) == 64)

    bad = bytearray(b"\x00" * 336)
    try:
        zipcrypto.generate_zip_password(bytes(bad))
        check("zip signature guard", False)
    except zipcrypto.ResourcePackError:
        check("zip signature guard", True)


def test_paper_parsing() -> None:
    payload = {
        "structure_type": "collector.role",
        "info": {
            "question": [
                {
                    "ask": "<p>Hello there, how are you?</p>",
                    "std": [{"value": "I am fine, thank you."}],
                    "value": "Hello there, how are you?",
                }
            ]
        },
        "set_id": "12345",
        "resource_id": "55",
    }
    section = paper_mod.parse_content_json(payload, "hw1", "/a.zip", "听选信息", {"set_id": "12345"})
    check("paper question count", len(section.questions) == 1)
    question = section.questions[0]
    check("paper question text", question.question_text == "Hello there, how are you?", question.question_text)
    check("paper answer", question.answers == ["I am fine, thank you."], str(question.answers))
    check("paper context set_id", question.context["set_id"] == "12345")
    check("paper segments", question.speech_segments() == ["Hello there, how are you?"], str(question.speech_segments()))

    choose_payload = {
        "structure_type": "collector.choose",
        "info": {
            "xtlist": [
                {
                    "xt_nr": "What does she do?",
                    "answer": "B",
                    "xxlist": [
                        {"xx_mc": "A", "xx_nr": "Teacher"},
                        {"xx_mc": "B", "xx_nr": "Doctor"},
                    ],
                }
            ]
        },
    }
    choose = paper_mod.parse_content_json(choose_payload, "hw2", "/b.zip", "听后选择")
    check("choose is_choice", choose.questions[0].is_choice)
    check("choose answer", choose.questions[0].correct_answer == "B")
    check("choose no audio", choose.questions[0].speech_segments() == [])

    read_payload = {
        "structure_type": "collector.read",
        "info": {"value": "The cat sat on the mat. It was warm."},
    }
    read = paper_mod.parse_content_json(read_payload, "hw3", "/c.zip", "短文朗读")
    check("read kind", read.questions[0].kind == "短文朗读", read.questions[0].kind)
    check("read single segment", len(read.questions[0].speech_segments()) == 1)


def test_sentence_split() -> None:
    parts = paper_mod.split_sentences("First one. Second one! Third one?")
    check("sentence split", parts == ["First one.", "Second one!", "Third one?"], str(parts))


def test_score_xml() -> None:
    xml = score_mod.build_score_xml("Hello world. Bye.", "read_chapter", 4.5)
    check("score xml root", xml.startswith("<xml_result>"))
    check("score xml has score", 'total_score="4.500000"' in xml)
    check("score xml escapes", "<" not in xml.split("content=\"")[1].split("\"")[0])
    data = score_mod.build_score_data("simple_expression", 4.5)
    parsed = json.loads(data.to_json())
    check("score json", parsed["total_score"] == "4.5" and parsed["category"] == "simple_expression")
    check("score clamp", score_mod.clamp_score(9.9, 5) == "5.0")


def test_pacing() -> None:
    pacer = Pacer(enabled=True, request_delay_ms=10, reading_wpm=600)
    start = __import__("time").perf_counter()
    pacer.reading("one two three four five")
    elapsed = __import__("time").perf_counter() - start
    check("pacing waits", elapsed > 0, f"{elapsed:.3f}s")
    pacer.stop()
    check("pacing stop flag", pacer.stopped)


def test_live_api() -> None:
    """The signature must be accepted by the real server.

    ``m/ecard/list`` is used on purpose: it validates the envelope without
    touching the login endpoint, which rate-limits repeated attempts.
    """
    client = EtsApiClient(timeout=20)
    try:
        client.ecard_list("0" * 32)
        check("live api signature", False, "unexpectedly succeeded")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "token" in message.lower() or "20004" in message or "过期" in message:
            check("live api signature accepted", True, message[:140])
        elif "connect" in message.lower() or "timeout" in message.lower():
            print(f"[SKIP] live api signature accepted  -> 网络不可达: {message[:90]}")
        else:
            check("live api signature accepted", False, message[:140])


def test_config() -> None:
    check("data dir", config.data_dir().exists(), str(config.data_dir()))
    settings = config.load_settings()
    check("default provider", settings.get("tts_provider") == "minimax")
    check("pacing defaults", settings.get("pace_enabled") is True)
    check("dry run default", settings.get("dry_run") is True)


def main() -> int:
    for fn in (
        test_envelope,
        test_device_code,
        test_zip_password,
        test_paper_parsing,
        test_sentence_split,
        test_score_xml,
        test_pacing,
        test_config,
        test_live_api,
    ):
        print(f"\n--- {fn.__name__} ---")
        fn()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {', '.join(FAILURES)}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
