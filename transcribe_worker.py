"""Transcribes speech one chunk at a time in its own process.

Whisper's memory use grows with audio length, so long videos are split into chunks.
Each finished chunk is saved, which lets a restarted worker continue where it stopped.
Running separately also means a crash here can't take down the web app.

Exit codes: 0 finished, 1 unexpected error, 2 the speech model could not be loaded.
"""
from __future__ import annotations

# On Windows, the DLLs that other packages load first can crash the speech engine,
# so load it before anything that might bring in its own native libraries.
try:
    import ctranslate2  # noqa: F401
except ImportError:
    pass

import json
import sys
import traceback
import wave
from pathlib import Path

import pipeline

SAMPLE_RATE = 16000


def say_progress(fraction: float) -> None:
    print(f"P {min(max(fraction, 0.0), 1.0):.4f}", flush=True)


def transcribe_chunk(model, wav: wave.Wave_read, chunk: dict, total: float, language: str | None) -> dict:
    import numpy as np

    wav.setpos(int(chunk["start"] * SAMPLE_RATE))
    raw = wav.readframes(int((chunk["end"] - chunk["start"]) * SAMPLE_RATE))
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    segments, info = model.transcribe(
        audio,
        language=language,
        word_timestamps=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    words = []
    for segment_index, segment in enumerate(segments):
        for word in segment.words or []:
            start = float(word.start) + chunk["start"]
            end = float(word.end) + chunk["start"]
            if chunk["keep_from"] <= (start + end) / 2 < chunk["keep_to"]:
                words.append({
                    "text": word.word.strip(),
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "confidence": round(float(word.probability), 3),
                    "segment": segment_index,
                })
        say_progress((chunk["start"] + float(segment.end)) / total)
    return {"language": str(getattr(info, "language", "") or ""), "words": words}


def main(wav_path: str, model_name: str, device: str, compute_type: str, work_dir: str, cpu_threads: str = "0") -> int:
    try:
        runtime = pipeline.windows_runtime()
        if runtime:
            print(f"R {runtime[0]}|{runtime[1]}", flush=True)
    except Exception:
        traceback.print_exc()
    folder = Path(work_dir)
    folder.mkdir(parents=True, exist_ok=True)
    with wave.open(wav_path, "rb") as wav:
        total = wav.getnframes() / SAMPLE_RATE
        chunks = pipeline.plan_chunks(total)
        remaining = [chunk for chunk in chunks if not pipeline.chunk_path(folder, chunk["index"]).exists()]
        if not remaining:
            return 0

        language = None
        for chunk in chunks:
            path = pipeline.chunk_path(folder, chunk["index"])
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["words"] and saved["language"]:
                    language = saved["language"]
                    break

        try:
            from faster_whisper import WhisperModel

            model = WhisperModel(model_name, device=device, compute_type=compute_type, cpu_threads=int(cpu_threads))
        except Exception:
            traceback.print_exc()
            return 2

        say_progress(remaining[0]["start"] / total if total else 0.0)
        for chunk in remaining:
            print(f"C {chunk['index']}", flush=True)
            result = transcribe_chunk(model, wav, chunk, total, language)
            if result["words"] and not language:
                language = result["language"]
            path = pipeline.chunk_path(folder, chunk["index"])
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
            say_progress(chunk["end"] / total)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(*sys.argv[1:7]))
    except Exception:
        traceback.print_exc()
        sys.exit(1)
