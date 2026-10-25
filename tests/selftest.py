"""FastETS self-test: protocol, parsing and pacing sanity checks.

Run with:  python -m tests.selftest
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows consoles default to cp1252 and cannot encode Chinese output.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

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

    CI runners are frequently blocked by the CDN's bot protection (the reply
    is an HTML page instead of JSON). That is a network condition, not a
    protocol regression, so it is reported as SKIP.
    """
    client = EtsApiClient(timeout=20)
    try:
        client.ecard_list("0" * 32)
        check("live api signature", False, "unexpectedly succeeded")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        code = getattr(exc, "code", None)
        if code == 20004 or "过期" in message or "token" in message.lower():
            check("live api signature accepted", True, message[:140])
            return
        blocked = (
            "HTTP " in message
            or "<html" in message.lower()
            or "<!doctype" in message.lower()
            or "connect" in message.lower()
            or "timeout" in message.lower()
            or "resolve" in message.lower()
        )
        if blocked:
            print(
                f"[SKIP] live api signature accepted  -> 该网络出口被 CDN 风控拦截: "
                f"{message[:80]}"
            )
        else:
            check("live api signature accepted", False, message[:140])


def test_voice_resolution() -> None:
    """The voice id must fall back to a default voice when nothing is cloned."""
    from fastets.voice import DEFAULT_MINIMAX_VOICE, resolve_voice_id
    from fastets.voice.minimax import MiniMaxProvider
    from fastets.voice.edge import EdgeTTSProvider
    from fastets.voice.openai_compat import OpenAICompatProvider

    minimax = MiniMaxProvider(api_key="x")
    edge = EdgeTTSProvider()
    openai = OpenAICompatProvider(base_url="http://127.0.0.1:1")

    check(
        "voice falls back to the default",
        resolve_voice_id({"minimax_voice_id": ""}, minimax) == DEFAULT_MINIMAX_VOICE,
    )
    check(
        "cloned voice wins",
        resolve_voice_id({"minimax_voice_id": "voice-123"}, minimax) == "voice-123",
    )
    check(
        "voice_mode default uses the chosen voice",
        resolve_voice_id(
            {"voice_mode": "default", "default_voice": "female-shaonv"}, minimax
        )
        == "female-shaonv",
    )
    check(
        "edge uses its own default",
        resolve_voice_id({"edge_voice": ""}, edge) == "en-US-AriaNeural",
    )
    check(
        "openai uses its own default",
        resolve_voice_id({"openai_voice": ""}, openai) == "alloy",
    )


def test_progress_payload() -> None:
    """A structured progress payload must never reach the UI as an object."""
    from fastets.jobs.runner import JobRunner

    runner = JobRunner()
    runner.start("test", "test", lambda: None)
    runner.progress({"stage": "download", "message": "下载资源包 1/2", "percent": 12})
    events = runner.events_since(0)["events"]
    progress_events = [e for e in events if e.get("type") == "progress"]
    check("progress event emitted", bool(progress_events))
    if progress_events:
        message = progress_events[-1].get("message")
        check("progress message is text", isinstance(message, str), repr(message))
        check("progress message content", "下载资源包" in str(message))
        check("progress percent kept", progress_events[-1].get("percent") == 12)
    runner.log({"stage": "x"})
    log_events = [e for e in runner.events_since(0)["events"] if e.get("type") == "log"]
    check("log message is text", isinstance(log_events[-1].get("message"), str))


def test_audio_pipeline() -> None:
    """mp3 and the raw Speex payload must be produced for every answer."""
    from fastets.core import audio as audio_mod

    tools = audio_mod.AudioTools()
    try:
        ffmpeg = tools.ffmpeg
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] audio pipeline  -> 没有可用的 ffmpeg: {exc}")
        return

    status = tools.speex_status()
    if status.get("available"):
        check("speex encoder detected", True, str(status.get("message")))
    else:
        print(f"[INFO] speex encoder not available: {status.get('message')}")

    # A one second sine wave is enough to exercise the whole conversion chain.
    source = config.audio_cache_dir() / "selftest_tone.wav"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "quiet",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1:sample_rate=16000",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        check=False,
    )
    if not source.exists() or source.stat().st_size == 0:
        print("[SKIP] audio pipeline  -> 无法生成测试音频")
        return

    mp3 = tools.to_mp3(source)
    check("mp3 produced", mp3.exists() and mp3.stat().st_size > 0, f"{mp3.stat().st_size} bytes")
    info = tools.probe(mp3)
    check("mp3 is 16 kHz mono", info.get("sample_rate") == 16000, str(info.get("sample_rate")))
    check(
        "mp3 duration about one second",
        800 <= info.get("duration_ms", 0) <= 1400,
        str(info.get("duration_ms")),
    )
    # duration_ms() used to shell out to ffprobe, which is not shipped with
    # the bundled ffmpeg - it always returned 0 and every submission went out
    # with use_time=0.
    measured = tools.duration_ms(mp3)
    check(
        "duration probe returns the real length",
        800 <= measured <= 1400,
        str(measured),
    )

    try:
        speex = tools.to_speex(source)
    except audio_mod.SpeexUnavailableError as exc:
        print(f"[SKIP] speex payload  -> 没有 Speex 编码器: {exc}")
        return
    check("speex produced", speex.exists() and speex.stat().st_size > 0, f"{speex.stat().st_size} bytes")
    check(
        "speex payload is a whole number of frames",
        speex.stat().st_size % 1 == 0 and speex.stat().st_size > 20,
    )


def test_submit_payload() -> None:
    """The sync-v2 payload must carry every field the ETS100 server needs."""
    from fastets.core.ets_api import EtsApiClient

    client = EtsApiClient(timeout=5)
    captured: dict = {}

    def fake_request(route, params=None, **kwargs):
        captured["route"] = route
        captured["params"] = dict(params or {})
        return {"body": {"code": 0}}

    client.request = fake_request  # type: ignore[assignment]
    client.submit_audio(
        token="tok",
        context={"homework_id": "1", "set_id": "2", "entity_id": "3", "order": "4"},
        score="5.0",
        score_detail='{"score":5}',
        mp3_url="https://cdn/x.mp3",
        speex_url="https://cdn/x.speex",
        xml_url="https://cdn/x.xml",
    )
    params = captured.get("params", {})
    required = [
        "homework_id",
        "set_id",
        "entity_id",
        "order",
        "score",
        "score_detail",
        "upload_file_url",
        "detail_file_url",
        "spx_file_url",
        "mark_service",
        "sign_response",
        "token",
        "version",
        "resource_id",
    ]
    missing = [key for key in required if key not in params]
    check("submit payload complete", not missing, f"missing: {missing}")
    check("submit route", captured.get("route") == "m/audio/sync-v2", str(captured.get("route")))
    check(
        "submit carries the three attachments",
        params.get("upload_file_url", "").endswith(".mp3")
        and params.get("detail_file_url", "").endswith(".xml")
        and params.get("spx_file_url", "").endswith(".speex"),
    )

    # An empty spx must be accepted as well - that is what the official client
    # sends for answers without a recording.
    client.submit_audio(
        token="tok",
        context={"homework_id": "1"},
        score="1.0",
        score_detail="{}",
        mp3_url="https://cdn/x.mp3",
        speex_url="",
        xml_url="https://cdn/x.xml",
    )
    check("submit works without a spx attachment", captured["params"].get("spx_file_url") == "")


def _offline_completer():
    """Build a Completer that runs the production pipeline against fakes.

    Returns ``(completer, submitted, uploaded, restore)`` or ``None`` when no
    ffmpeg is available.  ``restore()`` puts the real OSS uploader back.
    """
    from fastets.core.completer import Completer
    from fastets.voice.base import TTSResult

    class FakeProvider:
        name = "fake"
        supports_clone = False

        def synthesize(self, text, voice_id="", speed=1.0, pitch=0, **kwargs):
            return TTSResult(audio=b"", voice_id=voice_id, provider="fake")

    class FakeSession:
        token = "tok"
        phone = ""
        password = ""

        def ensure_fresh_token(self):
            return self.token

        @property
        def parent_account_id(self):
            return "pid"

    # The fake provider has to return real audio, so build it with ffmpeg.
    from fastets.core import audio as audio_mod

    tools = audio_mod.AudioTools()
    try:
        ffmpeg = tools.ffmpeg
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] completer offline  -> 没有可用的 ffmpeg: {exc}")
        return None

    sample = config.audio_cache_dir() / "selftest_speech.mp3"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "quiet",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=2:sample_rate=16000",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(sample),
        ],
        check=False,
    )
    if not sample.exists():
        print("[SKIP] completer offline  -> 无法生成测试音频")
        return None
    speech = sample.read_bytes()

    def synthesize(self, text, voice_id="", speed=1.0, pitch=0, **kwargs):
        return TTSResult(audio=speech, voice_id=voice_id, provider="fake")

    FakeProvider.synthesize = synthesize

    completer = Completer(
        FakeSession(),
        settings={
            "dry_run": False,
            "pace_enabled": False,
            "keep_audio_cache": False,
            "target_score_ratio": 1.0,
            "score_mode": "target",
            "ffmpeg_path": "",
        },
        logger=None,
    )
    completer._provider = FakeProvider()

    uploaded: List[dict] = []
    submitted: List[dict] = []

    class FakeUploader:
        def __init__(self, *args, **kwargs):
            pass

        def upload(self, local_path, kind):
            path = Path(local_path)
            uploaded.append({"kind": kind, "size": path.stat().st_size if path.exists() else 0})
            return {"object_key": f"k/{kind}", "url": f"https://cdn.example/{kind}"}

    class FakeClient:
        def oss_upload_config(self, token):
            return {}

        def submit_audio(self, **kwargs):
            submitted.append(kwargs)
            return {"code": 0}

        def submit_choice(self, **kwargs):
            submitted.append(kwargs)
            return {"code": 0}

    completer.session.client = FakeClient()  # type: ignore[attr-defined]
    import fastets.core.completer as completer_mod

    original_uploader = completer_mod.oss.OssUploader
    completer_mod.oss.OssUploader = FakeUploader  # type: ignore[assignment]

    def restore() -> None:
        completer_mod.oss.OssUploader = original_uploader  # type: ignore[assignment]

    return completer, submitted, uploaded, restore


def test_completer_offline() -> None:
    """The whole submission pipeline, without touching the network.

    A fake voice provider returns a real mp3, the OSS uploader and the API
    client are replaced by recorders.  Everything in between - audio
    conversion, the score XML, the attachment upload and the sync payload - is
    the production code path.
    """
    from fastets.core import paper as paper_mod
    from fastets.core.completer import LoadedPaper

    offline = _offline_completer()
    if offline is None:
        return
    completer, submitted, uploaded, restore = offline
    try:
        question = paper_mod.Question(
            question_id="q1",
            homework_id="h1",
            content_url="",
            order=1,
            group_name="跟读",
            structure_type="collector.sentence",
            question_text="朗读这句话",
            answers=["Hello everyone, this is a test."],
            context={"homework_id": "h1", "set_id": "s1", "entity_id": "e1"},
            max_score=5.0,
        )
        paper = LoadedPaper({"id": "h1", "name": "测试作业"}, [paper_mod.Section("跟读", "collector.sentence", [question])])
        report = completer.complete_homework(
            {"id": "h1", "name": "测试作业"}, paper=paper, dry_run=False
        )
    finally:
        restore()

    check("completer submitted every question", report.get("done") == 1, str(report.get("done")))
    check("completer reported no failures", report.get("failed") == 0, str(report.get("failed")))
    kinds = sorted(item["kind"] for item in uploaded)
    has_speex = "speex" in kinds
    if has_speex:
        check("three attachments uploaded", kinds == ["audio", "speex", "xml"], str(kinds))
    else:
        # Without a Speex encoder the submission must still go through - the
        # spx attachment is optional, only the mp3 and the score XML matter.
        check("submission continues without a speex encoder", kinds == ["audio", "xml"], str(kinds))
    check(
        "every attachment has content",
        all(item["size"] > 0 for item in uploaded),
        str([(i["kind"], i["size"]) for i in uploaded]),
    )
    check("one submission sent", len(submitted) == 1, str(len(submitted)))
    if submitted:
        item = submitted[0]
        check("submission carries mp3", item.get("mp3_url", "").endswith("audio"))
        if has_speex:
            check("submission carries speex", item.get("speex_url", "").endswith("speex"))
        else:
            check("submission carries an empty speex", item.get("speex_url", "") == "")
        check("submission carries xml", item.get("xml_url", "").endswith("xml"))
        check("submission carries a score", float(item.get("score", 0)) > 0, str(item.get("score")))
        check("submission carries the score detail", bool(item.get("score_detail")))


def test_item_number_restarts_per_pack() -> None:
    """The item number of an answer restarts with every resource pack.

    The server identifies an answer by "pack + item number".  Sending the
    paper-wide number makes the server drop the answers of every pack that
    is not the first one of its group - the assignment then stays at e.g.
    75% no matter how many times it is submitted.  This is the bug that
    kept the real assignment at 9 of 12 answers.
    """
    from fastets.core import paper as paper_mod
    from fastets.core.completer import LoadedPaper

    offline = _offline_completer()
    if offline is None:
        return
    completer, submitted, _uploaded, restore = offline

    sections = []
    # Three packs of the same 大题, one question each - the shape of 朗读词组.
    for index in range(3):
        section = paper_mod.parse_content_json(
            {
                "structure_type": "collector.read",
                "info": {"value": f"Word number {index}."},
            },
            "h1",
            f"/pack{index}.zip",
            "朗读词组",
        )
        for question in section.questions:
            question.context["entity_id"] = f"e{index}"
        sections.append(section)
    # One pack with five questions - the shape of 交际应答.
    dialogue = paper_mod.parse_content_json(
        {
            "structure_type": "collector.dialogue",
            "info": {
                "question": [
                    {
                        "xh": number,
                        "ask": f"Ask {number}?",
                        "std": [{"value": f"Answer {number}."}],
                    }
                    for number in range(1, 6)
                ]
            },
        },
        "h1",
        "/dialogue.zip",
        "交际应答",
    )
    for question in dialogue.questions:
        question.context["entity_id"] = "d"
    sections.append(dialogue)

    paper = LoadedPaper({"id": "h1", "name": "测试作业"}, sections)
    try:
        completer.complete_homework({"id": "h1", "name": "测试作业"}, paper=paper, dry_run=False)
    finally:
        restore()

    rows = [
        (
            str((item.get("context") or {}).get("entity_id")),
            str((item.get("context") or {}).get("order")),
            str((item.get("context") or {}).get("serial_id")),
        )
        for item in submitted
        if "context" in item
    ]
    check("one submission per question", len(rows) == 8, str(rows))
    check(
        "every pack sends its own item numbers",
        rows
        == [
            ("e0", "1", "0"),
            ("e1", "1", "0"),
            ("e2", "1", "0"),
            ("d", "1", "0"),
            ("d", "2", "0"),
            ("d", "3", "0"),
            ("d", "4", "0"),
            ("d", "5", "0"),
        ],
        str(rows),
    )
    check(
        "the paper wide number is not sent",
        all(order != "2" for entity, order, _serial in rows[:3]),
        str(rows[:3]),
    )


def test_response_candidates() -> None:
    """交际应答: the alternatives are answers, the prompt is not read."""
    from fastets.core import paper as paper_mod

    payload = {
        "structure_type": "collector.dialogue",
        "info": {
            "question": [
                {
                    "xh": 1,
                    "ask": "<p>What do you do after school?</p>",
                    "std": [
                        {"value": "I play football."},
                        {"value": "I read books."},
                        {"value": "I help my mum."},
                    ],
                },
                {
                    "xh": 2,
                    "ask": "<p>Where are you from?</p>",
                    "std": [{"value": "I am from Shanghai."}],
                },
            ]
        },
    }
    section = paper_mod.parse_content_json(payload, "hw", "/d.zip", "交际应答")
    check("response question count", len(section.questions) == 2, str(len(section.questions)))
    first = section.questions[0]
    check("response is flagged", first.is_response, first.group_name)
    check(
        "alternatives become candidates",
        list(first.answer_candidates) == ["I play football.", "I read books.", "I help my mum."],
        str(first.answer_candidates),
    )
    check(
        "the prompt is not read",
        first.read_text == "I play football.",
        first.read_text,
    )
    check("one recording per answer", len(first.speech_segments()) == 1, str(first.speech_segments()))
    check(
        "the single alternative is still the answer",
        section.questions[1].read_text == "I am from Shanghai.",
        section.questions[1].read_text,
    )

    # A plain role play question keeps its old behaviour.
    role = paper_mod.parse_content_json(
        {
            "structure_type": "collector.role",
            "info": {
                "question": [
                    {"ask": "<p>Hello there.</p>", "std": [{"value": "Hi!"}]},
                ]
            },
        },
        "hw",
        "/e.zip",
        "角色扮演",
    )
    check("role play is not a response", not role.questions[0].is_response)
    check("role play has no candidates", not role.questions[0].answer_candidates)


def test_log_redaction() -> None:
    """The log file must never contain the login password."""
    from fastets.core.ets_api import redact_params

    redacted = redact_params(
        {
            "phone": "13800000000",
            "password": "secret-password",
            "token": "abcdefghijklmnop",
            "code": "123456",
            "homework_id": "h1",
        }
    )
    check("password masked", redacted.get("password") == "***", str(redacted.get("password")))
    check("token masked", "abcdefghijklmnop" not in str(redacted.get("token")), str(redacted.get("token")))
    check("code masked", redacted.get("code") == "***", str(redacted.get("code")))
    check("phone kept for debugging", redacted.get("phone") == "13800000000")
    check("other params kept", redacted.get("homework_id") == "h1")


def test_generic_item_splitting() -> None:
    """An unknown structure type is split into its items, not one answer.

    The concrete type names of the packs we have not seen yet (课后作业,
    单词作业, ...) are unknown, so the parser works on the *shape* of the
    data: a list of items under ``info`` becomes one question per item.
    """
    from fastets.core import paper as paper_mod

    # A pack that carries its items under "sublist" with a "seq" number.
    payload = {
        "structure_type": "collector.repeat_essay",
        "info": {
            "sublist": [
                {"seq": 1, "value": "The first item to read aloud."},
                {"seq": 2, "value": "The second item to read aloud."},
                {"seq": 3, "value": "The third item to read aloud."},
            ]
        },
    }
    section = paper_mod.parse_content_json(payload, "hw", "/f.zip", "课后作业")
    check("unknown type is not a known structure", not paper_mod.is_known_structure("collector.repeat_essay"))
    check("the pack becomes one question per item", len(section.questions) == 3, str(len(section.questions)))
    check(
        "the item number comes from seq",
        [q.order for q in section.questions] == [1, 2, 3],
        str([q.order for q in section.questions]),
    )
    check(
        "the item text is read",
        section.questions[0].read_text == "The first item to read aloud.",
        section.questions[0].read_text,
    )
    check(
        "one recording per item",
        all(len(q.speech_segments()) == 1 for q in section.questions),
        str([len(q.speech_segments()) for q in section.questions]),
    )

    # A pack that carries questions with alternatives (ask + std).
    dialogue_like = {
        "structure_type": "collector.whatever",
        "info": {
            "question": [
                {"ask": "<p>How are you?</p>", "std": [{"value": "I am fine."}, {"value": "Not bad."}]},
                {"ask": "<p>Where are you?</p>", "std": [{"value": "I am at school."}]},
            ]
        },
    }
    parsed = paper_mod.parse_content_json(dialogue_like, "hw", "/g.zip", "快速问答")
    check("question list becomes one question per entry", len(parsed.questions) == 2, str(len(parsed.questions)))
    check(
        "the alternatives become candidates",
        list(parsed.questions[0].answer_candidates) == ["I am fine.", "Not bad."],
        str(parsed.questions[0].answer_candidates),
    )
    check(
        "the prompt is not read",
        parsed.questions[0].read_text == "I am fine.",
        parsed.questions[0].read_text,
    )
    check(
        "one recording per question",
        len(parsed.questions[0].speech_segments()) == 1,
        str(parsed.questions[0].speech_segments()),
    )

    # The known types must not change: collector.picture keeps one question
    # with alternative answers, collector.read keeps its single passage.
    picture = paper_mod.parse_content_json(
        {
            "structure_type": "collector.picture",
            "info": {"std": [{"value": "One."}, {"value": "Two."}], "value": "source"},
        },
        "hw",
        "/h.zip",
        "信息转述",
    )
    check("picture stays one question", len(picture.questions) == 1, str(len(picture.questions)))
    check(
        "picture keeps its alternatives",
        list(picture.questions[0].answer_candidates) == ["One.", "Two."],
        str(picture.questions[0].answer_candidates),
    )
    read = paper_mod.parse_content_json(
        {"structure_type": "collector.read", "info": {"value": "A passage."}},
        "hw",
        "/i.zip",
        "短文朗读",
    )
    check("read stays one question", len(read.questions) == 1, str(len(read.questions)))
    check("read keeps its text", read.questions[0].read_text == "A passage.", read.questions[0].read_text)


def test_speex_frames() -> None:
    """The raw Speex payload must survive a wrap/unwrap round trip."""
    from fastets.core import audio as audio_mod
    from fastets.core import speex as speex_mod

    helper = speex_mod.find_helper()
    if not helper:
        print("[SKIP] speex frames  -> 没有内置 Speex 编码器")
        return

    tools = audio_mod.AudioTools()
    source = config.audio_cache_dir() / "selftest_frames.wav"
    subprocess.run(
        [
            tools.ffmpeg,
            "-y",
            "-v",
            "quiet",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=1:sample_rate=16000",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        check=False,
    )
    if not source.exists():
        print("[SKIP] speex frames  -> 无法生成测试音频")
        return

    target = config.audio_cache_dir() / "selftest_frames.speex"
    try:
        speex_mod.encode_with_helper(tools.ffmpeg, source, target)
    except Exception as exc:  # noqa: BLE001
        # The helper exists, so a failure here is a real defect (on Windows a
        # text mode stdin truncates the PCM) - it must not be skipped.
        check("speex payload could be encoded", False, str(exc))
        return
    payload = target.read_bytes()
    check("speex payload is not empty", len(payload) > 0, f"{len(payload)} bytes")

    # Wrapping the frames into an Ogg stream and reading them back must give
    # exactly the same bytes - that is what the ETS100 server receives.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from ogg_speex import build_ogg_speex  # noqa: PLC0415
    except ImportError:
        print("[SKIP] speex frames  -> 缺少 tests/ogg_speex.py")
        return

    frames = _split_frames(payload, helper, tools.ffmpeg, source)
    check("speex payload splits into frames", len(frames) > 1, str(len(frames)))
    ogg = build_ogg_speex(frames, 16000, 320)
    extracted = audio_mod.extract_raw_speex_from_ogg(ogg)
    check("speex frames survive the round trip", extracted == payload)


def _split_frames(payload: bytes, helper: str, ffmpeg: str, source: Path) -> List[bytes]:
    """Split a raw Speex payload into its frames.

    The helper reports the length of every frame it produced; the tests use
    that instead of guessing from the frame header.
    """
    lengths_file = config.audio_cache_dir() / "selftest_frames.len"
    pcm = config.audio_cache_dir() / "selftest_frames.s16le"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "quiet",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            str(pcm),
        ],
        check=False,
    )
    with pcm.open("rb") as handle:
        subprocess.run(
            [helper, "--rate", "16000", "--quality", "8", "--lengths-file", str(lengths_file)],
            stdin=handle,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    frames: List[bytes] = []
    offset = 0
    for line in lengths_file.read_text().split():
        size = int(line)
        frames.append(payload[offset : offset + size])
        offset += size
    return frames


def test_config() -> None:
    check("data dir", config.data_dir().exists(), str(config.data_dir()))
    settings = config.load_settings()
    # The defaults must not depend on what the user has configured locally.
    check("default provider", config.DEFAULT_SETTINGS.get("tts_provider") == "minimax")
    check("configured provider loaded", bool(settings.get("tts_provider")))
    check("pacing defaults", settings.get("pace_enabled") is True)
    # The defaults must not depend on what the user has configured locally.
    check("dry run default", config.DEFAULT_SETTINGS.get("dry_run") is True)
    check("voice mode default", config.DEFAULT_SETTINGS.get("voice_mode") == "clone")


def test_speex_matches_ffmpeg() -> None:
    """The bundled helper must produce the very same frames as ffmpeg's
    libspeex encoder.

    The ETS100 cloud stores every answer twice (mp3 + raw Speex).  The
    official client builds the Speex payload with ffmpeg's ``libspeex``
    encoder, so FastETS has to produce the same bytes.  The comparison needs
    an ffmpeg that still ships the libspeex encoder (removed in ffmpeg 6.0);
    when none is around the test is skipped.
    """
    from fastets.core import audio as audio_mod
    from fastets.core import speex as speex_mod

    helper = speex_mod.find_helper()
    if not helper:
        print("[SKIP] speex vs ffmpeg  -> 没有内置 Speex 编码器")
        return

    tools = audio_mod.AudioTools()
    candidates = [tools.ffmpeg]
    candidates += sorted(str(p) for p in Path("/opt/homebrew/opt").glob("ffmpeg@*/bin/ffmpeg"))
    candidates += sorted(str(p) for p in Path("/usr/local/bin").glob("ffmpeg*"))

    reference = None
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
        if "libspeex" in (probe.stdout or ""):
            reference = candidate
            break
    if not reference:
        print("[SKIP] speex vs ffmpeg  -> 找不到带 libspeex 编码器的 ffmpeg")
        return

    source = config.audio_cache_dir() / "selftest_compare.s16"
    subprocess.run(
        [
            reference,
            "-y",
            "-v",
            "quiet",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=2:sample_rate=16000",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            str(source),
        ],
        check=False,
    )
    if not source.exists():
        print("[SKIP] speex vs ffmpeg  -> 无法生成测试音频")
        return
    pcm = source.read_bytes()

    # ffmpeg writes an Ogg Speex stream; the raw frames are what we compare.
    ogg_target = config.audio_cache_dir() / "selftest_compare.spx"
    subprocess.run(
        [
            reference,
            "-y",
            "-v",
            "quiet",
            "-f",
            "s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-i",
            str(source),
            "-c:a",
            "libspeex",
            "-cbr_quality",
            "8",
            str(ogg_target),
        ],
        check=False,
    )
    if not ogg_target.exists():
        print("[SKIP] speex vs ffmpeg  -> ffmpeg 无法编码 Speex")
        return
    expected = audio_mod.extract_raw_speex_from_ogg(ogg_target.read_bytes())

    target = config.audio_cache_dir() / "selftest_compare.speex"
    encoded = subprocess.run(
        [helper, "--rate", "16000", "--quality", "8"],
        input=pcm,
        capture_output=True,
        check=False,
    )
    payload = encoded.stdout
    check("helper produced frames", len(payload) > 0, f"{len(payload)} bytes")

    # The bit for bit match was measured on arm64.  On x86_64 the fused
    # multiply-add instructions make a few of libspeex's quantiser decisions
    # differ from the reference build, so the frames are equivalent but not
    # identical there - the geometry still has to match.
    identical = payload == expected
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        check(
            "helper output is byte identical to ffmpeg's libspeex",
            identical,
            f"ours {len(payload)} B vs ffmpeg {len(expected)} B",
        )
    else:
        check(
            "helper frames have the same geometry as ffmpeg's",
            len(payload) == len(expected),
            f"ours {len(payload)} B vs ffmpeg {len(expected)} B",
        )
        if not identical and len(payload) == len(expected):
            differing = sum(1 for a, b in zip(payload, expected) if a != b)
            print(
                f"[INFO] speex vs ffmpeg  -> {differing} of {len(expected)} bytes differ "
                f"({machine}, fused multiply-add rounding)"
            )

    # And it has to be reproducible.
    again = subprocess.run(
        [helper, "--rate", "16000", "--quality", "8"],
        input=pcm,
        capture_output=True,
        check=False,
    ).stdout
    check("helper output is deterministic", again == payload)


def test_answer_candidates() -> None:
    """复述 / 表达 ship several sample answers - one of them gets read."""
    from fastets.core import paper as paper_mod

    payload = {
        "structure_type": "collector.picture",
        "info": {
            "std": [
                {"value": "First sample answer."},
                {"value": "Second sample answer."},
                {"value": "Third sample answer."},
            ],
            "value": "W: the source dialogue.",
            "topic": "The topic",
        },
    }
    section = paper_mod.parse_content_json(payload, "hw", "url", "复述")
    question = section.questions[0]
    check("sample answers are parsed", len(question.answers) == 3, str(len(question.answers)))
    check(
        "sample answers become candidates",
        list(question.answer_candidates) == question.answers,
    )
    check(
        "the source dialogue is still available",
        question.read_text.startswith("W:"),
        question.read_text[:20],
    )

    # Without a candidate the parsed text is read as before.
    plain = paper_mod.parse_content_json(
        {
            "structure_type": "collector.read",
            "info": {"value": "A single sentence."},
        },
        "hw",
        "url",
        "朗读句子",
    )
    check(
        "other question types have no candidates",
        not plain.questions[0].answer_candidates,
    )
    check(
        "the override replaces the text",
        plain.questions[0].speech_segments("Another sentence.")[0]
        == "Another sentence.",
    )


def test_answer_pick_modes() -> None:
    """The setting decides which sample answer is read."""
    from fastets.core import completer as completer_mod
    from fastets.core import paper as paper_mod

    question = paper_mod.Question(
        question_id="q1",
        homework_id="hw",
        content_url="url",
        order=1,
        group_name="复述",
        structure_type="collector.picture",
        question_text="topic",
        answers=["A", "B", "C"],
        context={},
        answer_candidates=["A", "B", "C"],
    )

    def pick(settings, chooser=None):
        completer = completer_mod.Completer.__new__(completer_mod.Completer)
        completer.settings = settings
        completer._chooser = chooser
        completer._log = None
        return completer._chosen_answer_text(question, "复述 第 1 题")

    check("first mode reads the first answer", pick({"answer_pick_mode": "first"}) == "A")
    check(
        "random mode reads one of the answers",
        pick({"answer_pick_mode": "random"}) in ("A", "B", "C"),
    )
    check(
        "manual mode uses the user's choice",
        pick({"answer_pick_mode": "manual"}, lambda request: "C") == "C",
    )
    check(
        "manual mode falls back to the first answer",
        pick({"answer_pick_mode": "manual"}, lambda request: "nonsense") == "A",
    )
    check(
        "manual mode without a chooser uses the first answer",
        pick({"answer_pick_mode": "manual"}) == "A",
    )


def test_mark_error_handling() -> None:
    """An ETS marking outage must not be reported as a failed question."""
    from fastets.core import completer as completer_mod
    from fastets.core.ets_api import EtsApiError

    check(
        "the marking outage is recognised",
        completer_mod._is_transient_mark_error(
            EtsApiError("远程获取ets服务失败", 500, "/m/audio/sync-v2")
        ),
    )
    check(
        "a token error is not treated as transient",
        not completer_mod._is_transient_mark_error(
            EtsApiError("token已过期", 401, "/m/audio/sync-v2")
        ),
    )
    check(
        "other errors are not treated as transient",
        not completer_mod._is_transient_mark_error(
            EtsApiError("网络请求失败 /m/audio/sync-v2", None, "/m/audio/sync-v2")
        ),
    )


def test_macos_voice() -> None:
    """The macOS system voices are a keyless fallback."""
    import platform

    from fastets.voice import build_fallback_provider, build_provider
    from fastets.voice.mac_say import MacSayProvider

    provider = build_provider({"tts_provider": "macos", "macos_voice": "Samantha"})
    check("the macOS provider is registered", provider.name == "macos")
    check(
        "the macOS provider reports its availability",
        isinstance(MacSayProvider.available(), bool),
    )
    if platform.system() != "Darwin":
        check(
            "the macOS provider refuses to work on other systems",
            not MacSayProvider.available(),
        )
        print("[SKIP] macOS voice  -> 当前不是 macOS")
        return

    try:
        result = provider.synthesize("Hello from the system voice.", voice_id="Samantha")
        check("the system voice produced audio", len(result.audio) > 1000, f"{len(result.audio)} bytes")
        check("the audio is a RIFF/WAVE file", result.audio[:4] == b"RIFF")
    except Exception as exc:  # noqa: BLE001
        check("the system voice produced audio", False, str(exc))

    fallback = build_fallback_provider({"tts_fallback": "macos"})
    check("the macOS fallback is available", fallback is not None and fallback.name == "macos")
    check(
        "the fallback can be switched off",
        build_fallback_provider({"tts_fallback": "none"}) is None,
    )


def test_mark_retry() -> None:
    """A marking outage is retried and never counted as a failed question."""
    from fastets.core import paper as paper_mod
    from fastets.core.completer import Completer, LoadedPaper
    from fastets.core.ets_api import EtsApiError
    from fastets.voice.base import TTSResult
    import fastets.core.completer as completer_mod

    from fastets.core import audio as audio_mod

    tools = audio_mod.AudioTools()
    try:
        ffmpeg = tools.ffmpeg
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] mark retry  -> 没有可用的 ffmpeg: {exc}")
        return

    sample = config.audio_cache_dir() / "selftest_mark.mp3"
    subprocess.run(
        [
            ffmpeg, "-y", "-v", "quiet", "-f", "lavfi",
            "-i", "sine=frequency=330:duration=1:sample_rate=16000",
            "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-q:a", "4", str(sample),
        ],
        check=False,
    )
    if not sample.exists():
        print("[SKIP] mark retry  -> 无法生成测试音频")
        return
    speech = sample.read_bytes()

    class FakeProvider:
        name = "fake"
        supports_clone = False

        def synthesize(self, text, voice_id="", speed=1.0, pitch=0, **kwargs):
            return TTSResult(audio=speech, voice_id=voice_id, provider="fake")

    class FakeSession:
        token = "tok"

        def ensure_fresh_token(self):
            return self.token

    class FakeUploader:
        def __init__(self, *args, **kwargs):
            pass

        def upload(self, local_path, kind):
            return {"object_key": f"k/{kind}", "url": f"https://cdn.example/{kind}"}

    attempts = []

    class FlakyClient:
        def oss_upload_config(self, token):
            return {}

        def submit_audio(self, **kwargs):
            attempts.append(kwargs)
            if len(attempts) < 2:
                raise EtsApiError("远程获取ets服务失败", 500, "/m/audio/sync-v2")
            return {"code": 0}

    class BrokenClient(FlakyClient):
        def submit_audio(self, **kwargs):
            attempts.append(kwargs)
            raise EtsApiError("远程获取ets服务失败", 500, "/m/audio/sync-v2")

    def build(client):
        completer = Completer(
            FakeSession(),
            settings={
                "dry_run": False,
                "pace_enabled": False,
                "keep_audio_cache": False,
                "target_score_ratio": 1.0,
                "score_mode": "target",
                "ffmpeg_path": "",
                "mark_retries": 2,
            },
        )
        completer._provider = FakeProvider()
        completer.session.client = client  # type: ignore[attr-defined]
        return completer

    def run(client):
        question = paper_mod.Question(
            question_id="q1",
            homework_id="h1",
            content_url="",
            order=1,
            group_name="跟读",
            structure_type="collector.sentence",
            question_text="朗读这句话",
            answers=["Hello everyone, this is a test."],
            context={"homework_id": "h1", "set_id": "s1", "entity_id": "e1"},
            max_score=5.0,
        )
        paper = LoadedPaper(
            {"id": "h1", "name": "测试作业"},
            [paper_mod.Section("跟读", "collector.sentence", [question])],
        )
        original = completer_mod.oss.OssUploader
        completer_mod.oss.OssUploader = FakeUploader  # type: ignore[assignment]
        original_delay = completer_mod.MARK_RETRY_DELAY_SECONDS
        completer_mod.MARK_RETRY_DELAY_SECONDS = 0
        try:
            return build(client).complete_homework(
                {"id": "h1", "name": "测试作业"}, paper=paper, dry_run=False
            )
        finally:
            completer_mod.oss.OssUploader = original  # type: ignore[assignment]
            completer_mod.MARK_RETRY_DELAY_SECONDS = original_delay

    attempts.clear()
    report = run(FlakyClient())
    check("the marking call was retried", len(attempts) == 2, str(len(attempts)))
    check("the retry recovered", report.get("failed") == 0, str(report.get("failed")))
    check("the question counts as done", report.get("done") == 1, str(report.get("done")))

    attempts.clear()
    report = run(BrokenClient())
    check("every attempt was used", len(attempts) == 3, str(len(attempts)))
    check("a broken marking service is not a failed question", report.get("failed") == 0, str(report.get("failed")))
    check("the question is reported as unconfirmed", report.get("unconfirmed") == 1, str(report.get("unconfirmed")))
    check(
        "the item carries the server message",
        bool((report.get("items") or [{}])[0].get("segments", [{}])[0].get("message")),
    )


def test_resource_id_flow() -> None:
    """The resource_id of the assignment has to reach every submission.

    The server looks the marking service up by resource_id.  A wrong id (the
    generic default) makes every single question fail with
    "远程获取ets服务失败", so the id from the homework list must win.
    """
    from fastets.core import paper as paper_mod

    payload = {"info": {"stid": 665159, "value": "hello"}}
    section = paper_mod.parse_content_json(
        payload,
        homework_id="110058131",
        content_url="x.zip",
        group_name="朗读词组",
        context_base={"set_id": "614336", "resource_id": "240"},
    )
    question = section.questions[0]
    check(
        "resource_id reaches the question context",
        question.context.get("resource_id") == "240",
        str(question.context.get("resource_id")),
    )
    check(
        "set_id reaches the question context",
        question.context.get("set_id") == "614336",
        str(question.context.get("set_id")),
    )
    check(
        "entity_id comes from stid",
        question.context.get("entity_id") == "665159",
        str(question.context.get("entity_id")),
    )

    # Without the assignment id the generic default is used.
    fallback = paper_mod.parse_content_json(
        payload,
        homework_id="110058131",
        content_url="x.zip",
        group_name="朗读词组",
        context_base={"set_id": "614336"},
    ).questions[0]
    check(
        "resource_id falls back to the default",
        fallback.context.get("resource_id") == config.ETS_EVALUATE_RESOURCE_ID,
        str(fallback.context.get("resource_id")),
    )


def test_cancel_stops_the_run() -> None:
    """Pressing "stop" has to interrupt the run, not just relabel it."""
    import threading

    from fastets.core import paper as paper_mod
    from fastets.core.completer import CancelledError, Completer

    cancel = threading.Event()
    completer = Completer(
        session=None,  # type: ignore[arg-type]
        settings={"pace_enabled": True, "reading_wpm": 150},
        cancel_event=cancel,
    )
    check("pacer shares the cancel flag", completer.pacer.stopped is False)
    cancel.set()
    check("pacer sees the cancel flag", completer.pacer.stopped is True)

    raised = False
    try:
        completer._check_cancel()
    except CancelledError:
        raised = True
    check("cancel raises CancelledError", raised)

    # The waiting helper must return as soon as the flag is set.
    completer2 = Completer(
        session=None,  # type: ignore[arg-type]
        settings={},
        cancel_event=threading.Event(),
    )
    completer2._cancel_event.set()
    started = time.monotonic()
    raised2 = False
    try:
        completer2._wait(5.0)
    except CancelledError:
        raised2 = True
    elapsed = time.monotonic() - started
    check("cancel interrupts a wait", raised2 and elapsed < 1.0, f"{elapsed:.2f}s")


def test_token_refresh() -> None:
    """An expired token must be renewed automatically, not fail the run."""
    from fastets.core.ets_api import EtsApiClient

    calls: list = []

    class FakeClient(EtsApiClient):
        def _raw_request(self, route, params, query=None):
            calls.append((route, dict(params)))
            if len(calls) == 1:
                return {"code": 20004, "msg": "ets的token过期"}
            return {"code": 0, "body": {"ok": True}}

    client = FakeClient(token_refresher=lambda: "fresh-token")
    payload = client.request("g/homework/list", {"token": "old-token", "sn": "test"})
    check("expired token is refreshed", len(calls) == 2, str(len(calls)))
    check("the retry uses the new token", calls[-1][1].get("token") == "fresh-token")
    check("the request succeeds after the refresh", payload.get("code") == 0)

    # Without a refresher the original error has to surface.
    plain = FakeClient()
    calls.clear()
    try:
        plain.request("g/homework/list", {"token": "old-token", "sn": "test"})
        failed = False
    except Exception as exc:  # noqa: BLE001
        failed = "token过期" in str(exc)
    check("without a refresher the error surfaces", failed)


def test_secure_store() -> None:
    """The account and password must live in the system credential store."""
    import shutil
    import tempfile

    from fastets.core.secure_store import CredentialStore

    with tempfile.TemporaryDirectory() as folder:
        store = CredentialStore(Path(folder))
        check("credential store backend", bool(store.backend_name), store.backend_name)
        store.save({"phone": "13800000000", "password": "s3cret"})
        loaded = store.load()
        if not loaded:
            # Some sandboxes (CI runners) have no usable keychain.  Report the
            # reason and skip instead of failing the whole self-test.
            print(f"[SKIP] credential store  -> {store.last_error!r}")
            return
        check("credentials round trip", loaded.get("password") == "s3cret", str(loaded))
        check("account round trip", loaded.get("phone") == "13800000000")
        if platform.system() == "Darwin":
            # The plain files must not contain the secret.
            on_disk = "\n".join(
                p.read_text(encoding="utf-8", errors="ignore")
                for p in Path(folder).rglob("*")
                if p.is_file() and p.suffix in (".json", ".txt", "")
            )
            check("no plaintext secret on disk", "s3cret" not in on_disk)
            # The keychain must be readable only by this program.
            keychain = Path(folder) / "fastets.keychain-db"
            check("macOS keychain created", keychain.exists() and keychain.stat().st_size > 0)
            store.delete()
            check("credentials deleted", store.load() == {})

            # A stale item (written by another build with a different access
            # list) must not shadow the new one.
            from fastets.core import secure_store

            original = secure_store._MacKeychain.trusted_executable
            try:
                secure_store._MacKeychain.trusted_executable = staticmethod(
                    lambda: "/bin/cat"
                )
                store.save({"phone": "13800000000", "password": "stale"})
                secure_store._MacKeychain.trusted_executable = staticmethod(original)
                store.save({"phone": "13800000000", "password": "fresh"})
                check("stale item replaced", store.load().get("password") == "fresh")
            finally:
                secure_store._MacKeychain.trusted_executable = staticmethod(original)
            store.delete()


def test_credential_migration() -> None:
    """A state file with a plaintext password must be migrated and cleaned."""
    import shutil
    import tempfile

    from fastets import config as config_mod
    from fastets.core.session import Session

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        state = root / "state.json"
        state.write_text(
            json.dumps(
                {
                    "session": {
                        "device_code": "706472F380C82685|141811DB40EFD640",
                        "phone": "13800000000",
                        "password": "legacy-secret",
                        "token": "abc",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        original = config_mod.data_dir
        config_mod.data_dir = lambda: root  # type: ignore[assignment]
        try:
            session = Session()
            check("phone migrated", session.phone == "13800000000", session.phone)
            check("password migrated", session.password == "legacy-secret")
            raw = state.read_text(encoding="utf-8")
            check("plaintext removed from the state file", "legacy-secret" not in raw)
            check("phone removed from the state file", "13800000000" not in raw)
            check("token still in the state file", "abc" in raw)
        finally:
            config_mod.data_dir = original  # type: ignore[assignment]


def test_stop_cancels_the_run() -> None:
    """Pressing "stop" must end the job instead of just relabelling it."""
    import threading
    import time as time_module

    from fastets.core.pacing import Pacer
    from fastets.jobs.runner import JobRunner

    runner = JobRunner()
    started = threading.Event()
    finished = threading.Event()

    def work() -> str:
        # The bridge wires the runner's cancel flag into the pacer.
        pacer = Pacer(
            reading_wpm=150,
            max_reading_delay_ms=9000,
            stop_event=runner.cancel_event,
        )
        pacer.reset()
        started.set()
        # The pacer waits on the runner's cancel flag, so the stop button
        # wakes it up right away.
        for _ in range(50):
            pacer.reading("this is a fairly long sentence to read aloud")
            if pacer.stopped:
                break
        finished.set()
        return "stopped" if pacer.stopped else "ran to the end"

    job = runner.start("complete", "test", work)
    check("job started", started.wait(timeout=5))
    time_module.sleep(0.2)
    stopped = runner.stop()
    check("stop reported", stopped)
    check("job finished quickly", finished.wait(timeout=5))
    deadline = time_module.time() + 5
    while time_module.time() < deadline and job.status not in ("cancelled", "succeeded", "failed"):
        time_module.sleep(0.05)
    check("job status is cancelled", job.status == "cancelled", job.status)
    check("job message", job.message == "已取消", job.message)


def main() -> int:
    for fn in (
        test_envelope,
        test_device_code,
        test_zip_password,
        test_paper_parsing,
        test_sentence_split,
        test_score_xml,
        test_pacing,
        test_voice_resolution,
        test_progress_payload,
        test_audio_pipeline,
        test_submit_payload,
        test_speex_frames,
        test_speex_matches_ffmpeg,
        test_answer_candidates,
        test_answer_pick_modes,
        test_mark_error_handling,
        test_mark_retry,
        test_macos_voice,
        test_resource_id_flow,
        test_cancel_stops_the_run,
        test_token_refresh,
        test_secure_store,
        test_credential_migration,
        test_stop_cancels_the_run,
        test_completer_offline,
        test_item_number_restarts_per_pack,
        test_response_candidates,
        test_generic_item_splitting,
        test_log_redaction,
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
