"""Tests for flagging and rendering. Run them with: python -m pytest tests"""
import json
import re
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline  # noqa: E402

WORDLIST = (Path(__file__).resolve().parent.parent / "wordlist.txt").read_text()


def words(text, start=0.0, step=0.5):
    return [
        {"text": token, "start": start + index * step, "end": start + index * step + 0.4,
         "confidence": 0.9, "segment": 0}
        for index, token in enumerate(text.split())
    ]


def flagged(text):
    return [flag["text"] for flag in pipeline.scan_words(words(text), pipeline.parse_wordlist(WORDLIST))]


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def make_video(path, seconds=8, audio_offset=0.0, tracks=1, layout="stereo"):
    args = ["-f", "lavfi", "-i", f"testsrc2=s=320x180:r=24:d={seconds}"]
    for index in range(tracks):
        if audio_offset:
            args += ["-itsoffset", str(audio_offset)]
        args += ["-f", "lavfi", "-i", f"sine=f={300 + 100 * index}:r=48000:d={seconds}"]
    args += ["-map", "0:v"]
    for index in range(tracks):
        args += ["-map", f"{index + 1}:a"]
    args += ["-af", f"aformat=channel_layouts={layout}", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)]
    ffmpeg(*args)


def level(path, start, length, track=0):
    """Return mean volume in dB for a section of an audio track."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path),
         "-map", f"0:a:{track}", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", result.stderr)
    return float(match.group(1)) if match else float("-inf")


def tone_level(path, start, length, frequency):
    """Return volume in dB near one frequency, which tells a bleep from the source tone."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(length), "-i", str(path), "-map", "0:a:0",
         "-af", f"bandpass=f={frequency}:width_type=q:w=20,volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"mean_volume: (-?[\d.]+|-inf) dB", result.stderr)
    return float(match.group(1)) if match else float("-inf")


@pytest.mark.parametrize("text,expected", [
    ("what the FUCK, man", ["FUCK,"]),
    ("you motherfucker!", ["motherfucker!"]),
    ("that's bull-shit", ["bull-shit"]),
    ("son of a bitch here", ["son of a bitch"]),
    ("she said kill yourself today", ["kill yourself"]),
    ("he said f*** and s**t", ["f***", "s**t"]),
    ("he said [ __ ] again", ["__"]),
    ("Hello there, Dick’s here", []),
])
def test_scan_finds_expected_words(text, expected):
    assert flagged(text) == expected


@pytest.mark.parametrize("text", [
    "the assignment was classic",
    "a shell of a scunthorpe bass hello",
    "we saw a shiitake and a cocktail",
    "the hellish retardant",
])
def test_scan_avoids_substring_false_positives(text):
    assert flagged(text) == []


def test_flag_times_cover_whole_phrase():
    found = pipeline.scan_words(words("hey son of a bitch ok"), pipeline.parse_wordlist(WORDLIST))
    assert found[0]["start"] == 0.5 and found[0]["end"] == 2.4
    assert found[0]["decision"] == "undecided"


def test_censor_word_everywhere_handles_each_existing_state():
    transcript = words("Damn, the damn dog son of a damn cat DAMN! damned camera")
    # Word positions: 0 Damn, | 2 damn | 5 son of a damn (phrase) | 9 cat | 10 DAMN!
    flags = [
        {"id": "kept", "start": 0.0, "end": 0.4, "text": "Damn,", "decision": "keep"},
        {"id": "done", "start": 1.0, "end": 1.4, "text": "damn", "decision": "censor"},
        {"id": "phrase", "start": 2.0, "end": 3.9, "text": "son of a damn", "decision": "keep"},
    ]
    result = pipeline.censor_word_everywhere(transcript, flags, 2)
    assert result["word"] == "damn" and result["matches"] == 4
    assert result["changed"] == {"kept": "keep"}
    assert flags[0]["decision"] == "censor"
    assert next(f for f in flags if f["id"] == "phrase")["decision"] == "keep"
    assert [(f["text"], f["decision"]) for f in result["added"]] == [("damn", "censor"), ("DAMN!", "censor")]
    assert all(f["source"] == "manual" for f in result["added"])
    assert "damned" not in [f["text"] for f in result["added"]]


def test_censor_word_everywhere_rejects_punctuation_only_items():
    with pytest.raises(ValueError):
        pipeline.censor_word_everywhere(words("hello -- there"), [], 1)


def test_keep_word_everywhere_only_changes_that_words_flags():
    transcript = words("Damn, the damn dog son of a damn cat DAMN! damned camera")
    flags = [
        {"id": "undecided", "start": 0.0, "end": 0.4, "text": "Damn,", "decision": "undecided"},
        {"id": "censored", "start": 1.0, "end": 1.4, "text": "damn", "decision": "censor"},
        {"id": "phrase", "start": 2.0, "end": 3.9, "text": "son of a damn", "decision": "censor"},
        {"id": "kept", "start": 5.0, "end": 5.4, "text": "DAMN!", "decision": "keep"},
        {"id": "other", "start": 5.5, "end": 5.9, "text": "damned", "decision": "censor"},
    ]
    result = pipeline.keep_word_everywhere(transcript, flags, 2)
    assert result["matches"] == 4 and result["still_censored"] == 1
    assert result["changed"] == {"undecided": "undecided", "censored": "censor"}
    assert {flag["id"]: flag["decision"] for flag in flags} == {
        "undecided": "keep", "censored": "keep", "phrase": "censor", "kept": "keep", "other": "censor",
    }


def test_ranges_only_include_censored_flags_and_merge():
    flags = [
        {"start": 1.0, "end": 1.5, "decision": "censor"},
        {"start": 1.6, "end": 2.0, "decision": "censor"},
        {"start": 3.0, "end": 3.2, "decision": "keep"},
        {"start": 4.0, "end": 4.2, "decision": "undecided"},
        {"start": 9.9, "end": 12.0, "decision": "censor"},
    ]
    assert pipeline.censor_ranges(flags, 0.1, 10.0) == [(0.9, 2.1), (9.8, 10.0)]


def test_mute_silences_only_requested_range(tmp_path):
    source, output = tmp_path / "in.mp4", tmp_path / "out.mp4"
    make_video(source)
    pipeline.render(source, output, pipeline.probe(source), [(3.0, 4.0)], "mute")
    assert level(output, 3.2, 0.6) < -80
    assert level(output, 1.0, 1.0) > -30
    assert level(output, 5.0, 1.0) > -30


def test_many_ranges_render(tmp_path):
    source, output = tmp_path / "in.mp4", tmp_path / "out.mp4"
    make_video(source, seconds=30)
    ranges = [(index * 0.09, index * 0.09 + 0.03) for index in range(300)]
    pipeline.render(source, output, pipeline.probe(source), ranges, "mute")
    assert output.exists()


@pytest.mark.parametrize("mode", ["mute", "bleep"])
def test_hundreds_of_ranges_are_batched_and_all_censored(tmp_path, mode):
    source, output = tmp_path / "in.mp4", tmp_path / "out.mp4"
    make_video(source, seconds=120)
    ranges = [(1.0 + index * 0.3, 1.0 + index * 0.3 + 0.15) for index in range(352)]
    info = pipeline.probe(source)
    graph = pipeline.build_filter(info["audio"], ranges, mode)
    assert all(part.count("between(") <= pipeline.RANGES_PER_FILTER for part in graph.split("enable="))
    pipeline.render(source, output, info, ranges, mode)
    # Check ranges in the first and last batches, then audio after the final range.
    for start, _ in (ranges[0], ranges[175], ranges[-1]):
        if mode == "mute":
            assert level(output, start + 0.04, 0.07) < -60
        else:
            assert tone_level(output, start + 0.04, 0.07, 300) < tone_level(output, start + 0.04, 0.07, 1000)
    after = ranges[-1][1] + 2
    assert level(output, after, 3) > -30
    assert tone_level(output, after, 3, 1000) < -45


def test_render_reports_rising_progress_below_100_until_finished(tmp_path):
    source, output = tmp_path / "in.mp4", tmp_path / "out.mp4"
    make_video(source, seconds=20)
    progress = []
    pipeline.render(source, output, pipeline.probe(source), [(3.0, 4.0)], "mute", progress.append)
    assert progress and progress == sorted(progress)
    assert max(progress) <= 0.99


def test_scan_reports_progress():
    progress = []
    pipeline.scan_words(words("hello " * 1200), pipeline.parse_wordlist(WORDLIST), progress.append)
    assert progress == [0.0, 500 / 1200, 1000 / 1200]


def test_gaps_are_the_opposite_of_ranges():
    assert pipeline._gaps_between([(1.0, 2.0), (3.0, 4.0)]) == [(0.0, 1.0), (2.0, 3.0), (4.0, pipeline.FAR_FUTURE)]
    assert pipeline._gaps_between([(0.0, 2.0)]) == [(2.0, pipeline.FAR_FUTURE)]


def test_ffmpeg_error_message_shortens_long_lines(tmp_path):
    with pytest.raises(pipeline.MediaError) as error:
        pipeline._run_ffmpeg(["-f", "lavfi", "-i", "sine=d=1", "-af", "volume=enable='" + "a" * 2000 + "'", str(tmp_path / "o.mp4")])
    assert "a" * 400 not in str(error.value)


def test_bleep_replaces_source_tone(tmp_path):
    source, output = tmp_path / "in.mp4", tmp_path / "out.mp4"
    make_video(source)
    pipeline.render(source, output, pipeline.probe(source), [(3.0, 4.0)], "bleep")
    assert tone_level(output, 3.2, 0.6, 1000) > -30
    assert tone_level(output, 3.2, 0.6, 300) < -45
    assert tone_level(output, 1.0, 1.0, 1000) < -50
    assert tone_level(output, 1.0, 1.0, 300) > -30


def test_video_stream_is_copied_and_duration_kept(tmp_path):
    source, output = tmp_path / "in.mp4", tmp_path / "out.mp4"
    make_video(source)
    info = pipeline.probe(source)
    pipeline.render(source, output, info, [(3.0, 4.0)], "mute")

    def packet_hashes(path):
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "framemd5", "-"],
            capture_output=True, text=True, check=True,
        )
        return [line.split(",")[-1].strip() for line in result.stdout.splitlines() if not line.startswith("#")]

    assert packet_hashes(source) == packet_hashes(output)
    assert abs(pipeline.probe(output)["duration"] - info["duration"]) < 0.1


def test_every_audio_track_is_censored(tmp_path):
    source, output = tmp_path / "in.mp4", tmp_path / "out.mp4"
    make_video(source, tracks=2)
    info = pipeline.probe(source)
    assert len(info["audio"]) == 2
    pipeline.render(source, output, info, [(3.0, 4.0)], "mute")
    assert len(pipeline.probe(output)["audio"]) == 2
    for track in (0, 1):
        assert level(output, 3.2, 0.6, track) < -80
        assert level(output, 1.0, 1.0, track) > -30


@pytest.mark.parametrize("extension", [".mkv", ".webm", ".avi", ".mov"])
def test_other_containers_render(tmp_path, extension):
    source, output = tmp_path / f"in{extension}", tmp_path / f"out{extension}"
    codec = ["-c:v", "libvpx", "-b:v", "200k", "-c:a", "libopus"] if extension == ".webm" else ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "libmp3lame" if extension == ".avi" else "aac"]
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=6", "-f", "lavfi", "-i", "sine=f=300:r=48000:d=6", *codec, str(source))
    pipeline.render(source, output, pipeline.probe(source), [(2.0, 3.0)], "mute")
    assert level(output, 2.2, 0.6) < -60
    assert level(output, 4.0, 1.0) > -30


def test_extracted_audio_keeps_render_clock_when_audio_starts_late(tmp_path):
    """Transcript times must point at the same moment the renderer will mute."""
    source, wav, output = tmp_path / "in.mp4", tmp_path / "speech.wav", tmp_path / "out.mp4"
    make_video(source, seconds=6, audio_offset=1.0)
    info = pipeline.probe(source)
    pipeline.extract_audio(source, wav, info["duration"])
    assert level(wav, 0.1, 0.7) < -80
    assert level(wav, 1.3, 0.5) > -30
    pipeline.render(source, output, info, [(3.0, 4.0)], "mute")
    assert level(output, 3.2, 0.6) < -80
    assert level(output, 2.0, 0.8) > -30
    assert level(output, 4.3, 0.8) > -30


def test_no_audio_is_reported_clearly(tmp_path):
    source = tmp_path / "silent.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=1", "-c:v", "libx264", "-preset", "ultrafast", str(source))
    with pytest.raises(pipeline.MediaError, match="no audio"):
        pipeline.probe(source)


FAKE_WHISPER = r"""
import json, os
from pathlib import Path

class Word:
    def __init__(self, word, start, end):
        self.word, self.start, self.end, self.probability = word, start, end, 0.9

class Segment:
    def __init__(self, end, words):
        self.end, self.words = end, words

class Info:
    language = "en"

class WhisperModel:
    def __init__(self, *args, **kwargs):
        if os.environ.get("FAKE_LOAD_FAILS"):
            raise RuntimeError("download blocked")
        self.settings = kwargs
        if os.environ.get("FAKE_CRASH_ON_LOAD"):
            os.abort()

    def transcribe(self, audio, **options):
        assert not isinstance(audio, str) and audio.dtype.name == "float32"
        assert options["word_timestamps"] is True
        # The test audio stores which 10-second block each sample belongs to.
        start = round(float(audio[0]) * 32768) * 10
        with open(os.environ["FAKE_CALLS"], "a") as calls:
            calls.write(json.dumps([start, options.get("language")]) + "\n")
        if os.environ.get("FAKE_NEEDS_FLOAT32") and self.settings["compute_type"] != "float32":
            os.abort()
        if os.environ.get("FAKE_CRASH_AT") == str(start):
            marker = Path(os.environ["FAKE_CRASH_MARKER"])
            if os.environ.get("FAKE_CRASH_ALWAYS") or not marker.exists():
                marker.touch()
                os.abort()
        length = len(audio) / 16000
        words = [Word(f" w{start + i}", float(i), i + 0.4) for i in range(int(length))]
        return iter([Segment(length, words)]), Info()
"""


@pytest.fixture
def long_wav(tmp_path, monkeypatch):
    fake = tmp_path / "fake"
    fake.mkdir()
    (fake / "faster_whisper.py").write_text(FAKE_WHISPER)
    monkeypatch.setenv("PYTHONPATH", str(fake))
    monkeypatch.setenv("FAKE_CALLS", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FAKE_CRASH_MARKER", str(tmp_path / "crashed"))
    job = tmp_path / "job"
    job.mkdir()
    path = job / "speech.wav"
    seconds = 1500
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        for block in range(seconds // 10):
            output.writeframes(struct.pack("<h", block) * 160000)
    return path, seconds, tmp_path / "calls.log"


def calls_made(log):
    return [json.loads(line)[0] for line in log.read_text().splitlines()]


def test_chunks_cover_all_audio_without_gaps():
    chunks = pipeline.plan_chunks(1500)
    assert [chunk["start"] for chunk in chunks] == [0, 600, 1200]
    assert chunks[-1]["end"] == 1500 and chunks[-1]["keep_to"] == float("inf")
    for first, second in zip(chunks, chunks[1:]):
        assert first["keep_to"] == second["keep_from"] and first["end"] > second["keep_from"]
    assert len(pipeline.plan_chunks(30)) == 1
    assert pipeline.plan_chunks(0) == []


def test_long_audio_is_split_and_merged_without_lost_or_repeated_words(long_wav):
    path, seconds, log = long_wav
    progress = []
    words, language = pipeline.transcribe_audio(path, "small.en", on_progress=progress.append)
    assert language == "en" and progress[-1] == 1.0
    assert calls_made(log) == [0, 600, 1200]
    assert [word["text"] for word in words] == [f"w{second}" for second in range(seconds)]
    assert [word["start"] for word in words] == [float(second) for second in range(seconds)]
    assert words == sorted(words, key=lambda word: word["start"])
    assert not (path.parent / "transcript-small.en").exists()
    # Later chunks reuse the first chunk's language instead of detecting it again.
    assert [json.loads(line)[1] for line in log.read_text().splitlines()] == [None, "en", "en"]


def test_worker_crash_restarts_and_keeps_finished_chunks(long_wav, monkeypatch):
    path, seconds, log = long_wav
    monkeypatch.setenv("FAKE_CRASH_AT", "600")
    words, _ = pipeline.transcribe_audio(path, "small.en")
    assert calls_made(log) == [0, 600, 600, 1200]
    assert len(words) == seconds


def test_repeated_crash_reports_location_and_retry_resumes(long_wav, monkeypatch):
    path, seconds, log = long_wav
    monkeypatch.setenv("FAKE_CRASH_AT", "600")
    monkeypatch.setenv("FAKE_CRASH_ALWAYS", "1")
    with pytest.raises(pipeline.MediaError) as error:
        pipeline.transcribe_audio(path, "small.en")
    message = str(error.value)
    assert "00:10:00" in message and "1 of 3 parts are saved" in message
    assert "Fatal Python error" in message and "faster_whisper.py" in message
    assert "Fatal Python error" in (path.parent / "transcript-small.en" / "worker.log").read_text()
    monkeypatch.delenv("FAKE_CRASH_AT")
    monkeypatch.delenv("FAKE_CRASH_ALWAYS")
    words, _ = pipeline.transcribe_audio(path, "small.en")
    assert calls_made(log) == [0] + [600] * 6 + [1200]
    assert len(words) == seconds


def test_crash_moves_to_next_engine_settings(long_wav, monkeypatch):
    path, seconds, log = long_wav
    monkeypatch.setenv("FAKE_NEEDS_FLOAT32", "1")
    words, _ = pipeline.transcribe_audio(path, "small.en")
    assert len(words) == seconds
    # The standard settings crash before transcribing, then 32-bit math finishes every part.
    assert calls_made(log) == [0, 0, 600, 1200]


def test_crash_while_starting_is_reported_as_startup(long_wav, monkeypatch):
    path, _, log = long_wav
    monkeypatch.setenv("FAKE_CRASH_ON_LOAD", "1")
    with pytest.raises(pipeline.MediaError) as error:
        pipeline.transcribe_audio(path, "small.en")
    assert "while starting the speech engine" in str(error.value)
    assert "All 4 engine settings were tried" in str(error.value)
    text = (path.parent / "transcript-small.en" / "worker.log").read_text()
    assert "basic CPU instructions" in text


def test_model_load_failure_is_explained_without_retrying(long_wav, monkeypatch):
    path, _, log = long_wav
    monkeypatch.setenv("FAKE_LOAD_FAILS", "1")
    with pytest.raises(pipeline.MediaError, match=r"(?s)could not be loaded.*download blocked"):
        pipeline.transcribe_audio(path, "small.en")
    assert not log.exists()


def test_boundary_word_heard_by_both_chunks_is_kept_once(tmp_path):
    first = {"language": "en", "words": [{"text": "hello", "start": 604.0, "end": 605.1, "confidence": 1, "segment": 0}]}
    second = {"language": "en", "words": [
        {"text": "Hello,", "start": 604.9, "end": 605.4, "confidence": 1, "segment": 0},
        {"text": "there", "start": 605.5, "end": 606.0, "confidence": 1, "segment": 0},
    ]}
    pipeline.chunk_path(tmp_path, 0).write_text(json.dumps(first))
    pipeline.chunk_path(tmp_path, 1).write_text(json.dumps(second))
    words, _ = pipeline.merge_chunks(tmp_path, 2)
    assert [word["text"] for word in words] == ["hello", "there"]
    assert [word["segment"] for word in words] == [0, 1]


def write_worker(tmp_path, monkeypatch, body):
    worker = tmp_path / "fake_worker.py"
    worker.write_text("import os, sys\n" + body)
    monkeypatch.setattr(pipeline, "WORKER_PATH", worker)


def test_old_windows_runtime_gets_specific_instructions(long_wav, tmp_path, monkeypatch):
    path, _, _ = long_wav
    write_worker(tmp_path, monkeypatch,
                 "print(r'R 14.24.28127.4|C:\\Windows\\System32\\MSVCP140.dll', flush=True)\nos.abort()\n")
    with pytest.raises(pipeline.MediaError) as error:
        pipeline.transcribe_audio(path, "small.en")
    message = str(error.value)
    assert "14.24.28127.4" in message and pipeline.VC_RUNTIME_COMMAND in message
    assert "engine settings" not in message


def test_current_windows_runtime_still_tries_other_settings(long_wav, tmp_path, monkeypatch):
    path, _, _ = long_wav
    write_worker(tmp_path, monkeypatch,
                 "print(r'R 14.44.35211.0|C:\\Windows\\System32\\MSVCP140.dll', flush=True)\nos.abort()\n")
    with pytest.raises(pipeline.MediaError) as error:
        pipeline.transcribe_audio(path, "small.en")
    assert "All 4 engine settings" in str(error.value) and "14.44.35211.0" in str(error.value)


def test_crash_details_show_the_crashing_thread_not_other_threads(tmp_path):
    log = tmp_path / "worker.log"
    log.write_text(
        "Windows fatal exception: access violation\n\n"
        "Thread 0x1 (most recent call first):\n"
        '  File "threading.py", line 359 in wait\n'
        '  File "tqdm\\_monitor.py", line 69 in run\n\n'
        "Current thread 0x2 (most recent call first):\n"
        '  File "ctranslate2_load.py", line 689 in __init__\n'
        '  File "transcribe_worker.py", line 78 in main\n\n'
    )
    details = pipeline._crash_details(log, 0)
    assert "access violation" in details and "ctranslate2_load.py" in details
    assert "tqdm" not in details


@pytest.mark.parametrize("version,expected", [
    ("14.24.28127.4", True), ("14.39.0.0", True), ("14.40.33810.0", False), ("14.51.36247.0", False), ("bad", False),
])
def test_runtime_version_check(version, expected):
    assert pipeline._runtime_is_too_old(version) is expected


def test_exit_codes_are_described():
    assert "out of memory" in pipeline._describe_exit(0xC0000017)
    assert "0xC0000005" in pipeline._describe_exit(0xC0000005)
    assert pipeline._describe_exit(1) == "exit code 1"
