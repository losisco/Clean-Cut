"""Transcription, flagging, and audio rendering for Clean Cut.

This module has no Flask dependency, so it can be tested or scripted on its own.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

BROWSER_CONTAINERS = {".mp4", ".m4v", ".mov", ".webm"}
BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis"}
AUDIO_ENCODERS = {
    ".mp4": "aac",
    ".m4v": "aac",
    ".mov": "aac",
    ".mkv": "aac",
    ".webm": "libopus",
    ".avi": "libmp3lame",
}
WORKER_PATH = Path(__file__).resolve().parent / "transcribe_worker.py"
# Whisper needs about 3 GB per hour of audio just to prepare it, so work in short chunks.
CHUNK_SECONDS = 600
CHUNK_OVERLAP = 10.0
# The Windows engine is built with a toolset whose mutexes crash on older runtimes.
MINIMUM_VC_RUNTIME = (14, 40)
VC_RUNTIME_COMMAND = "winget install -e --id Microsoft.VCRedist.2015+.x64"
# Tried in order when the speech engine crashes before finishing any new audio.
ENGINE_PROFILES = [
    {"name": "standard"},
    {"name": "32-bit math", "compute_type": "float32"},
    {"name": "alternate math library", "env": {"CT2_USE_MKL": "0"}},
    {"name": "basic CPU instructions, one thread", "compute_type": "float32", "cpu_threads": 1,
     "env": {"CT2_USE_MKL": "0", "CT2_FORCE_CPU_ISA": "GENERIC"}},
]
RANGES_PER_FILTER = 40
FAR_FUTURE = 1e9
BLEEP_HZ = 1000
# FFmpeg's sine source is already quiet (about -18 dB), so this lifts it to about -12 dB.
BLEEP_VOLUME = 2.0
# Short audio frames keep each cut within about 5 ms of the requested time.
FILTER_FRAME_SAMPLES = 256

ProgressFn = Callable[[float], None]


class MediaError(RuntimeError):
    """A problem that can be shown directly to the person using the app."""


# ---------------------------------------------------------------------------
# Probing and audio extraction
# ---------------------------------------------------------------------------

def probe(path: str | Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise MediaError("This file could not be read as a video. It may be damaged or use an unsupported format.")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [s for s in streams if s.get("codec_type") == "video"]
    subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
    if not audio:
        raise MediaError("This video has no audio track, so there is nothing to transcribe.")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, ValueError):
        duration = max((float(s.get("duration", 0) or 0) for s in streams), default=0.0)
    return {
        "duration": duration,
        "audio": [
            {
                "codec": s.get("codec_name", ""),
                "sample_rate": int(s.get("sample_rate", 48000) or 48000),
                "channels": int(s.get("channels", 2) or 2),
                "layout": s.get("channel_layout", ""),
            }
            for s in audio
        ],
        "video_codec": video[0].get("codec_name", "") if video else "",
        "has_subtitles": bool(subtitles),
    }


def needs_preview(path: str | Path, info: dict) -> bool:
    """Return True when a browser is unlikely to play the original upload."""
    ext = Path(path).suffix.lower()
    return not (
        ext in BROWSER_CONTAINERS
        and info["video_codec"] in BROWSER_VIDEO
        and info["audio"][0]["codec"] in BROWSER_AUDIO
    )


def _run_ffmpeg(args: list[str], duration: float = 0.0, on_progress: ProgressFn | None = None) -> None:
    command = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", *args]
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as stderr:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, text=True)
        assert process.stdout is not None
        for line in process.stdout:
            if on_progress and duration > 0 and line.startswith("out_time_us="):
                value = line.split("=", 1)[1].strip()
                if value.lstrip("-").isdigit():
                    # FFmpeg may still be finishing the file when all the audio is processed.
                    on_progress(min(max(int(value) / 1_000_000 / duration, 0.0), 0.99))
        code = process.wait()
        if code != 0:
            stderr.seek(0)
            lines = [line if len(line) <= 300 else line[:150] + " … " + line[-100:]
                     for line in stderr.read().splitlines()]
            tail = "\n".join(lines)[-1200:]
            raise MediaError(f"FFmpeg could not process this file.\n{tail}")


def extract_audio(source: str | Path, wav_path: str | Path, duration: float = 0.0,
                  on_progress: ProgressFn | None = None) -> None:
    """Extract 16 kHz mono speech audio on the same clock FFmpeg uses to render.

    ``aresample=async=1:first_pts=0`` pads a late audio start and fills gaps, so
    transcript times line up with the times used for muting.
    """
    _run_ffmpeg(
        ["-i", str(source), "-map", "0:a:0", "-vn",
         "-af", "aresample=async=1:first_pts=0",
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav_path)],
        duration, on_progress,
    )


def make_preview(source: str | Path, target: str | Path, duration: float = 0.0,
                 on_progress: ProgressFn | None = None) -> None:
    _run_ffmpeg(
        ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0",
         "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", str(target)],
        duration, on_progress,
    )


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

def plan_chunks(total: float) -> list[dict]:
    """Split audio into overlapping chunks, each keeping words up to the overlap's midpoint."""
    if total <= 0:
        return []
    count = max(math.ceil((total - CHUNK_OVERLAP) / CHUNK_SECONDS), 1)
    chunks = []
    for index in range(count):
        start = index * CHUNK_SECONDS
        boundary = start + CHUNK_SECONDS + CHUNK_OVERLAP / 2
        chunks.append({
            "index": index,
            "start": float(start),
            "end": total if index == count - 1 else float(start + CHUNK_SECONDS + CHUNK_OVERLAP),
            "keep_from": 0.0 if index == 0 else start + CHUNK_OVERLAP / 2,
            "keep_to": math.inf if index == count - 1 else boundary,
        })
    return chunks


def chunk_path(folder: str | Path, index: int) -> Path:
    return Path(folder) / f"chunk-{index:05d}.json"


def merge_chunks(folder: str | Path, count: int) -> tuple[list[dict], str]:
    words: list[dict] = []
    language = ""
    next_segment = 0
    for index in range(count):
        saved = json.loads(chunk_path(folder, index).read_text(encoding="utf-8"))
        language = language or (saved["language"] if saved["words"] else "")
        numbering: dict[int, int] = {}
        for word in saved["words"]:
            # A word sitting right on a chunk boundary can be heard by both chunks.
            if words and normalize(words[-1]["text"]) == normalize(word["text"]) and word["start"] < words[-1]["end"]:
                continue
            if word["segment"] not in numbering:
                numbering[word["segment"]] = next_segment
                next_segment += 1
            words.append({**word, "segment": numbering[word["segment"]]})
    return words, language


def _describe_exit(code: int) -> str:
    meanings = {
        0xC0000005: "a memory access error",
        0xC000001D: "an instruction this processor doesn't support",
        0xC0000017: "the computer running out of memory",
        0xC000012D: "the computer running out of memory",
        0xC0000409: "an internal failure in the speech engine",
    }
    if code < 0:
        return f"exit signal {-code}"
    label = f"exit code 0x{code:08X}" if code > 0xFFFF else f"exit code {code}"
    return f"{label}, {meanings[code]}" if code in meanings else label


def _timestamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole // 60 % 60:02d}:{whole % 60:02d}"


def _log_tail(log_path: Path, start: int) -> str:
    with log_path.open("rb") as log:
        log.seek(start)
        lines = log.read().decode("utf-8", "replace").strip().splitlines()
    return lines[-1][:400] if lines else ""


def _crash_details(log_path: Path, start: int) -> str:
    """Return the crash type and the crashing thread's innermost code locations."""
    with log_path.open("rb") as log:
        log.seek(start)
        lines = log.read().decode("utf-8", "replace").splitlines()
    headline = next((line.strip() for line in lines
                     if line.startswith(("Windows fatal exception", "Fatal Python error"))), "")
    # Python lists every thread, and only the "Current thread" section shows the crash.
    first = next((index + 1 for index, line in enumerate(lines) if line.startswith("Current thread")), None)
    frames = []
    if first is not None:
        for line in lines[first:]:
            if not line.startswith("  File "):
                break
            frames.append(line.strip())
    return "\n".join(part for part in [headline, *frames[:5]] if part)


def windows_runtime() -> tuple[str, str] | None:
    """Return the version and path of the loaded Visual C++ runtime on Windows."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32")
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    handle = kernel.GetModuleHandleW("msvcp140.dll")
    if not handle:
        return None
    name = ctypes.create_unicode_buffer(32768)
    kernel.GetModuleFileNameW(handle, name, 32768)

    version = ctypes.WinDLL("version")
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
    size = version.GetFileVersionInfoSizeW(name.value, None)
    data = ctypes.create_string_buffer(size)
    if not size or not version.GetFileVersionInfoW(name.value, 0, size, data):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    fixed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32 * 13)).contents
    high, low = fixed[2], fixed[3]
    return f"{high >> 16}.{high & 0xFFFF}.{low >> 16}.{low & 0xFFFF}", name.value


def _runtime_is_too_old(version: str) -> bool:
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except ValueError:
        return False
    return (major, minor) < MINIMUM_VC_RUNTIME


def _saved_profile(folder: Path) -> int:
    try:
        return min(max(int((folder / "engine-profile.txt").read_text()), 0), len(ENGINE_PROFILES) - 1)
    except (OSError, ValueError):
        return 0


def transcribe_audio(wav_path: str | Path, model_name: str, device: str = "cpu",
                     compute_type: str = "int8",
                     on_progress: ProgressFn | None = None) -> tuple[list[dict], str]:
    """Return ``(words, language)``, restarting the worker if it crashes.

    A crash with no new progress moves to the next engine profile. Finished chunks
    are saved beside the audio, so a later retry continues from them.
    """
    wav_path = Path(wav_path)
    folder = wav_path.parent / f"transcript-{model_name}"
    folder.mkdir(exist_ok=True)
    log_path = folder / "worker.log"
    with wave.open(str(wav_path), "rb") as wav:
        total = wav.getnframes() / wav.getframerate()
    chunks = plan_chunks(total)
    base_environment = {**os.environ, "HF_HUB_DISABLE_SYMLINKS_WARNING": "1", "PYTHONIOENCODING": "utf-8"}

    profile_index = _saved_profile(folder)
    while True:
        profile = ENGINE_PROFILES[profile_index]
        finished_before = sum(chunk_path(folder, chunk["index"]).exists() for chunk in chunks)
        log_start = log_path.stat().st_size if log_path.exists() else 0
        current = None
        runtime = ""
        command = [sys.executable, "-X", "faulthandler", str(WORKER_PATH), str(wav_path), model_name, device,
                   profile.get("compute_type", compute_type), str(folder), str(profile.get("cpu_threads", 0))]
        with log_path.open("ab") as log:
            log.write(f"\n--- Engine settings: {profile['name']} ---\n".encode("utf-8"))
            log.flush()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, text=True,
                                       env={**base_environment, **profile.get("env", {})})
            assert process.stdout is not None
            for line in process.stdout:
                kind, _, value = line.strip().partition(" ")
                if kind == "P" and on_progress:
                    on_progress(float(value))
                elif kind == "C":
                    current = int(value)
                elif kind == "R":
                    runtime = value
            code = process.wait()
        if code == 0:
            break
        if code == 2:
            raise MediaError(
                "The speech model could not be loaded. The first run needs internet access to download it. "
                f"Check your connection and try again.\nDetails: {_log_tail(log_path, log_start)}"
            )
        finished_after = sum(chunk_path(folder, chunk["index"]).exists() for chunk in chunks)
        runtime_version, _, runtime_path = runtime.partition("|")
        if runtime_version and _runtime_is_too_old(runtime_version):
            raise MediaError(
                f"The speech engine crashed, and this PC is using an old Microsoft Visual C++ runtime, "
                f"version {runtime_version}. The engine needs {MINIMUM_VC_RUNTIME[0]}.{MINIMUM_VC_RUNTIME[1]} or later.\n\n"
                f"Run this in PowerShell, then restart Clean Cut and choose Try again:\n{VC_RUNTIME_COMMAND}\n\n"
                f"Runtime in use: {runtime_path}\nLog: {log_path}"
            )
        if finished_after > finished_before:
            (folder / "engine-profile.txt").write_text(str(profile_index))
            continue
        profile_index += 1
        if profile_index >= len(ENGINE_PROFILES):
            where = "while starting the speech engine" if current is None else f"near {_timestamp(chunks[current]['start'])}"
            details = _crash_details(log_path, log_start)
            raise MediaError(
                f"Transcription stopped unexpectedly {where} ({_describe_exit(code)}). "
                f"All {len(ENGINE_PROFILES)} engine settings were tried. "
                f"{finished_after} of {len(chunks)} parts are saved, so Try again continues from there."
                + (f"\n\n{details}" if details else "")
                + (f"\n\nVisual C++ runtime: {runtime_version} at {runtime_path}" if runtime_version else "")
                + f"\n\nLog: {log_path}"
            )

    words, language = merge_chunks(folder, len(chunks))
    if on_progress:
        on_progress(1.0)
    shutil.rmtree(folder, ignore_errors=True)
    return words, language


# ---------------------------------------------------------------------------
# Word list and flagging
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    raw: str
    category: str
    tokens: tuple[str, ...]
    patterns: tuple[re.Pattern, ...]


def normalize(word: str) -> str:
    text = unicodedata.normalize("NFKC", word).lower().replace("’", "'")
    return re.sub(r"^[^\w*]+|[^\w*]+$", "", text)


def _word_forms(word: str) -> list[str]:
    """Forms to test, so "bull-shit" matches both "bullshit" and "shit"."""
    base = normalize(word)
    forms = [base]
    if "-" in base:
        forms.append(base.replace("-", ""))
        forms.extend(part for part in base.split("-") if part)
    return forms


def parse_wordlist(text: str) -> list[Entry]:
    entries: list[Entry] = []
    category = "Flagged"
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            category = line[1:-1].strip() or "Flagged"
            continue
        tokens = tuple(re.sub(r"[^\w*'-]", "", part.lower()) for part in line.split())
        tokens = tuple(token for token in tokens if token.replace("*", ""))
        key = " ".join(tokens)
        if not tokens or key in seen:
            continue
        seen.add(key)
        patterns = tuple(
            re.compile("^" + ".*".join(re.escape(piece) for piece in token.split("*")) + "$")
            for token in tokens
        )
        entries.append(Entry(line, category, tokens, patterns))
    return entries


_CENSORED = re.compile(r"\w\*+|\*+\w|_{2,}")


def _entry_matches_at(entry: Entry, words: list[dict], index: int, forms: list[list[str]]) -> bool:
    length = len(entry.tokens)
    if index + length > len(words):
        return False
    if length == 1:
        return any(entry.patterns[0].match(form) for form in forms[index])
    return all(
        entry.patterns[offset].match(forms[index + offset][0])
        for offset in range(length)
    )


def scan_words(words: list[dict], entries: Iterable[Entry], on_progress: ProgressFn | None = None) -> list[dict]:
    """Find word-list matches and words the transcriber printed with asterisks.

    Longer phrases win, and matches never overlap.
    """
    forms = [_word_forms(word["text"]) for word in words]
    exact: dict[str, list[Entry]] = {}
    wildcard: list[Entry] = []
    for entry in sorted(entries, key=lambda item: -len(item.tokens)):
        if "*" in entry.tokens[0]:
            wildcard.append(entry)
        else:
            exact.setdefault(entry.tokens[0], []).append(entry)

    flags: list[dict] = []
    index = 0
    while index < len(words):
        if on_progress and index % 500 == 0:
            on_progress(index / len(words))
        candidates: list[Entry] = list(wildcard)
        for form in forms[index]:
            candidates.extend(exact.get(form, []))
        candidates.sort(key=lambda item: -len(item.tokens))
        match = next((e for e in candidates if _entry_matches_at(e, words, index, forms)), None)
        if match:
            length = len(match.tokens)
            flags.append(_make_flag(words, index, length, match.category, "wordlist"))
            index += length
        elif _CENSORED.search(words[index]["text"]):
            flags.append(_make_flag(words, index, 1, "Censored in transcript", "censored"))
            index += 1
        else:
            index += 1
    return flags


def context_for(words: list[dict], first: int, last: int, span: int = 7) -> str:
    start = max(first - span, 0)
    end = min(last + span + 1, len(words))
    text = " ".join(word["text"] for word in words[start:end])
    return ("… " if start > 0 else "") + text + (" …" if end < len(words) else "")


def _make_flag(words: list[dict], index: int, length: int, reason: str, source: str) -> dict:
    last = index + length - 1
    return {
        "start": words[index]["start"],
        "end": words[last]["end"],
        "text": " ".join(word["text"] for word in words[index:last + 1]),
        "reason": reason,
        "source": source,
        "context": context_for(words, index, last),
        "decision": "undecided",
    }


def censor_word_everywhere(words: list[dict], flags: list[dict], word_index: int) -> dict:
    """Censor every use of the word at ``word_index``. ``flags`` is updated in place.

    Capitalization and surrounding punctuation are ignored, but other forms are
    not: "damn" won't match "damned". A longer phrase flag keeps its own decision.
    The word gets its own flag inside that phrase, so only the word is censored.
    """
    target = normalize(words[word_index]["text"])
    if not target:
        raise ValueError("This item can't be matched because it has no letters or numbers.")
    matches = 0
    changed: dict[str, str] = {}
    added: list[dict] = []
    for index, word in enumerate(words):
        if normalize(word["text"]) != target:
            continue
        matches += 1
        middle = (word["start"] + word["end"]) / 2
        covering = [flag for flag in flags if flag["start"] <= middle <= flag["end"]]
        if any(flag["decision"] == "censor" for flag in covering):
            continue
        same_word = next((flag for flag in covering if normalize(flag["text"]) == target), None)
        if same_word:
            changed[same_word["id"]] = same_word["decision"]
            same_word["decision"] = "censor"
        else:
            flag = _make_flag(words, index, 1, "Added from transcript", "manual")
            flag["decision"] = "censor"
            added.append(flag)
    return {"word": target, "matches": matches, "changed": changed, "added": added}


def keep_word_everywhere(words: list[dict], flags: list[dict], word_index: int) -> dict:
    """Mark every flag for the word at ``word_index`` as Keep. ``flags`` is updated in place.

    Matching works like ``censor_word_everywhere``. A longer flag that contains the
    word keeps its own decision, so ``still_censored`` counts uses that stay censored.
    """
    target = normalize(words[word_index]["text"])
    if not target:
        raise ValueError("This item can't be matched because it has no letters or numbers.")
    matches = 0
    still_censored = 0
    changed: dict[str, str] = {}
    for word in words:
        if normalize(word["text"]) != target:
            continue
        matches += 1
        middle = (word["start"] + word["end"]) / 2
        covering = [flag for flag in flags if flag["start"] <= middle <= flag["end"]]
        for flag in covering:
            if normalize(flag["text"]) == target and flag["decision"] != "keep":
                changed[flag["id"]] = flag["decision"]
                flag["decision"] = "keep"
        if any(flag["decision"] == "censor" for flag in covering):
            still_censored += 1
    return {"word": target, "matches": matches, "changed": changed, "still_censored": still_censored}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def censor_ranges(flags: Iterable[dict], padding: float, duration: float) -> list[tuple[float, float]]:
    """Return sorted, padded, merged ranges for flags marked as censored."""
    ranges = []
    for flag in flags:
        if flag.get("decision") != "censor":
            continue
        start = max(float(flag["start"]) - padding, 0.0)
        end = float(flag["end"]) + padding
        if duration > 0:
            end = min(end, duration)
        if end > start:
            ranges.append((start, end))
    ranges.sort()
    merged: list[tuple[float, float]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _layout(audio: dict) -> str:
    layout = audio.get("layout") or ""
    if layout and layout != "unknown":
        return layout
    return f"{audio.get('channels', 2)}c"


def _silence_chain(ranges: list[tuple[float, float]]) -> str:
    """Build volume filters that silence the ranges, in small batches.

    Recent FFmpeg builds reject a single expression with a couple hundred terms.
    """
    filters = []
    for first in range(0, len(ranges), RANGES_PER_FILTER):
        batch = ranges[first:first + RANGES_PER_FILTER]
        expression = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in batch)
        filters.append(f"volume=enable='{expression}':volume=0")
    return ",".join(filters)


def _gaps_between(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    gaps = []
    previous = 0.0
    for start, end in ranges:
        if start > previous:
            gaps.append((previous, start))
        previous = end
    gaps.append((previous, FAR_FUTURE))
    return gaps


def build_filter(audio_tracks: list[dict], ranges: list[tuple[float, float]], mode: str) -> str:
    silence = _silence_chain(ranges)
    parts = []
    for index, track in enumerate(audio_tracks):
        source = f"[0:a:{index}]asetnsamples=n={FILTER_FRAME_SAMPLES}:p=0,{silence}"
        if mode == "bleep":
            layout = _layout(track)
            rate = track["sample_rate"]
            parts.append(f"{source},aformat=channel_layouts={layout}[muted{index}]")
            # The tone plays constantly, then everything outside the censored ranges is silenced.
            parts.append(
                f"sine=frequency={BLEEP_HZ}:sample_rate={rate}:samples_per_frame={FILTER_FRAME_SAMPLES},"
                f"volume={BLEEP_VOLUME},{_silence_chain(_gaps_between(ranges))},"
                f"aformat=sample_rates={rate}:channel_layouts={layout}[tone{index}]"
            )
            parts.append(f"[muted{index}][tone{index}]amix=inputs=2:duration=first:normalize=0[out{index}]")
        else:
            parts.append(f"{source}[out{index}]")
    return ";".join(parts)


def _filter_script_option() -> str:
    """Return the option that reads a filter graph from a file on this FFmpeg."""
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    match = re.match(r"ffmpeg version n?(\d+)\.", result.stdout)
    # Development builds have no release number and are newer than any release.
    if match and int(match.group(1)) < 7:
        return "-filter_complex_script"
    return "-/filter_complex"


def render(source: str | Path, target: str | Path, info: dict, ranges: list[tuple[float, float]],
           mode: str = "mute", on_progress: ProgressFn | None = None) -> None:
    """Write a copy with edited audio. Video and subtitles are copied unchanged."""
    if mode not in {"mute", "bleep"}:
        raise ValueError("Mode must be mute or bleep.")
    if not ranges:
        raise MediaError("Mark at least one flag as Censor before rendering.")
    target = Path(target)
    extension = Path(source).suffix.lower()
    encoder = AUDIO_ENCODERS.get(extension, "aac")
    bitrate = "384k" if max(track["channels"] for track in info["audio"]) > 2 else "192k"
    # A file avoids Windows' limit on command length when there are many ranges.
    filter_file = target.with_name(f".rendering-{target.stem}.filter.txt")
    filter_file.write_text(build_filter(info["audio"], ranges, mode), encoding="utf-8")
    args = [
        "-i", str(source),
        _filter_script_option(), str(filter_file),
        "-map", "0:v?",
    ]
    for index in range(len(info["audio"])):
        args += ["-map", f"[out{index}]", f"-map_metadata:s:a:{index}", f"0:s:a:{index}"]
    args += [
        "-map", "0:s?", "-map", "0:t?",
        "-c:v", "copy", "-c:s", "copy", "-c:t", "copy",
        "-c:a", encoder, "-b:a", bitrate,
    ]
    if extension in {".mp4", ".m4v", ".mov"}:
        args += ["-movflags", "+faststart"]
    temporary = target.with_name(f".rendering-{target.stem}{target.suffix}")
    args.append(str(temporary))
    try:
        _run_ffmpeg(args, info.get("duration", 0.0), on_progress)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        filter_file.unlink(missing_ok=True)
