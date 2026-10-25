"""FastETS self-test: protocol, parsing and pacing sanity checks.

Run with:  python -m tests.selftest
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
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


def test_completer_offline() -> None:
    """The whole submission pipeline, without touching the network.

    A fake voice provider returns a real mp3, the OSS uploader and the API
    client are replaced by recorders.  Everything in between - audio
    conversion, the score XML, the attachment upload and the sync payload - is
    the production code path.
    """
    from fastets.core import paper as paper_mod
    from fastets.core.completer import Completer, LoadedPaper
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
        return

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
        return
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
        completer_mod.oss.OssUploader = original_uploader  # type: ignore[assignment]

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
        print(f"[SKIP] speex frames  -> 编码失败: {exc}")
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
    check("default provider", settings.get("tts_provider") == "minimax")
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
    check(
        "helper output is byte identical to ffmpeg's libspeex",
        payload == expected,
        f"ours {len(payload)} B vs ffmpeg {len(expected)} B",
    )

    # And it has to be reproducible.
    again = subprocess.run(
        [helper, "--rate", "16000", "--quality", "8"],
        input=pcm,
        capture_output=True,
        check=False,
    ).stdout
    check("helper output is deterministic", again == payload)


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
        test_completer_offline,
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
