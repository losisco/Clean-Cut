"""Clean Cut: find, review, and censor language in a video's audio track."""
from __future__ import annotations

import csv
import faulthandler
import hashlib
import io
import json
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, url_for)
from werkzeug.utils import secure_filename

import pipeline

ROOT = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
MODELS = {
    "base.en": "Fastest, English only",
    "small.en": "Balanced, English only",
    "medium.en": "More accurate, English only",
    "large-v3": "Most accurate and slowest, any language",
}
DEFAULT_MODEL = "small.en"
DECISIONS = {"undecided", "censor", "keep"}
# Each step's key, label, and whether its progress can be measured as a percentage.
STEPS = [
    ("read", "Read the video", True),
    ("audio", "Extract the audio", True),
    ("model", "Load the speech model", False),
    ("transcribe", "Transcribe speech", True),
    ("preview", "Prepare a browser preview", True),
    ("flags", "Find flagged words", True),
]


class JobStore:
    """Stores each job as a JSON file beside its video."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, job_id: str) -> Path:
        if len(job_id) != 32 or not all(char in "0123456789abcdef" for char in job_id):
            abort(404)
        return self.directory / job_id

    def load(self, job_id: str) -> dict:
        with self.lock:
            file = self.path(job_id) / "job.json"
            if not file.exists():
                abort(404)
            return json.loads(file.read_text(encoding="utf-8"))

    def save(self, job: dict) -> None:
        with self.lock:
            folder = self.path(job["id"])
            temporary = folder / "job.json.tmp"
            temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            temporary.replace(folder / "job.json")

    def update(self, job_id: str, **changes) -> dict:
        with self.lock:
            job = self.load(job_id)
            job.update(changes)
            self.save(job)
            return job

    def all(self) -> list[dict]:
        with self.lock:
            jobs = []
            for file in self.directory.glob("*/job.json"):
                jobs.append(json.loads(file.read_text(encoding="utf-8")))
        return sorted(jobs, key=lambda job: job["created"], reverse=True)


def create_app(data_dir: Path | None = None, wordlist_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    data = Path(data_dir or os.environ.get("CLEAN_CUT_DATA", ROOT / "data"))
    wordlist_file = Path(wordlist_path or data / "wordlist.txt")
    store = JobStore(data / "jobs")
    workers = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clean-cut")
    render_progress: dict[str, float] = {}
    save_progress: dict[str, dict] = {}
    device = os.environ.get("WHISPER_DEVICE", "cpu")
    compute_type = os.environ.get("WHISPER_COMPUTE", "int8")

    if not wordlist_file.exists():
        wordlist_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "wordlist.txt", wordlist_file)

    # Work interrupted by a restart cannot continue, so mark it for retry.
    for job in store.all():
        changed = False
        if job["status"] in {"queued", "processing"}:
            _stop_active_steps(job)
            job.update(status="failed", error="The app stopped before this video finished processing.")
            changed = True
        render_state = job.get("render") or {}
        if render_state.get("status") == "rendering":
            render_state.update(status="failed", error="The app stopped before this render finished.")
            changed = True
        if changed:
            store.save(job)

    def source_path(job: dict) -> Path:
        return store.path(job["id"]) / f"source{job['extension']}"

    def set_step(job_id: str, key: str, status: str, progress: float | None = None) -> None:
        with store.lock:
            job = store.load(job_id)
            for step in job.get("steps", []):
                if step["key"] == key:
                    step["status"] = status
                    if progress is not None:
                        step["progress"] = round(progress, 4)
                    if status == "active":
                        percent = "" if step["progress"] is None else f", {int(step['progress'] * 100)}%"
                        job["stage"] = f"{step['label']}{percent}"
            store.save(job)

    def step_reporter(job_id: str, key: str):
        last = {"time": 0.0}

        def report(fraction: float) -> None:
            now = time.monotonic()
            if now - last["time"] >= 0.5:
                last["time"] = now
                set_step(job_id, key, "active", fraction)

        return report

    def read_entries() -> list[pipeline.Entry]:
        return pipeline.parse_wordlist(wordlist_file.read_text(encoding="utf-8"))

    def process(job_id: str) -> None:
        folder = store.path(job_id)
        wav = folder / "speech.wav"
        try:
            job = store.update(job_id, status="processing", stage="Starting", error=None, steps=_new_steps())
            source = source_path(job)

            set_step(job_id, "read", "active", 0.0)
            info = pipeline.probe(source)
            has_preview = pipeline.needs_preview(source, info)
            with store.lock:
                current = store.load(job_id)
                current["info"] = info
                if not has_preview:
                    current["steps"] = [step for step in current["steps"] if step["key"] != "preview"]
                store.save(current)
            set_step(job_id, "read", "done", 1.0)

            set_step(job_id, "audio", "active", 0.0)
            pipeline.extract_audio(source, wav, info["duration"], step_reporter(job_id, "audio"))
            set_step(job_id, "audio", "done", 1.0)

            set_step(job_id, "model", "active")
            report_transcription = step_reporter(job_id, "transcribe")
            model_ready = {"value": False}

            def on_transcribe(fraction: float) -> None:
                # The worker's first progress report means the model has finished loading.
                if not model_ready["value"]:
                    model_ready["value"] = True
                    set_step(job_id, "model", "done", 1.0)
                    set_step(job_id, "transcribe", "active", fraction)
                report_transcription(fraction)

            words, language = pipeline.transcribe_audio(wav, job["model"], device, compute_type, on_transcribe)
            if not model_ready["value"]:
                set_step(job_id, "model", "done", 1.0)
            set_step(job_id, "transcribe", "done", 1.0)

            preview_name = None
            if has_preview:
                set_step(job_id, "preview", "active", 0.0)
                pipeline.make_preview(source, folder / "preview.mp4", info["duration"],
                                      step_reporter(job_id, "preview"))
                preview_name = "preview.mp4"
                set_step(job_id, "preview", "done", 1.0)

            set_step(job_id, "flags", "active", 0.0)
            flags = pipeline.scan_words(words, read_entries(), step_reporter(job_id, "flags"))
            for flag in flags:
                flag["id"] = uuid.uuid4().hex[:12]
            set_step(job_id, "flags", "done", 1.0)
            store.update(job_id, status="ready", stage="Ready", progress=1.0, words=words,
                         language=language, flags=flags, preview=preview_name)
        except Exception as error:  # Shown on the job page so a failed job can be retried.
            app.logger.exception("Processing failed for %s", job_id)
            with store.lock:
                current = store.load(job_id)
                _stop_active_steps(current)
                current.update(status="failed", error=_friendly_error(error))
                store.save(current)
        finally:
            wav.unlink(missing_ok=True)

    def render_signature(job: dict, mode: str, padding_ms: int) -> str:
        ranges = pipeline.censor_ranges(job.get("flags", []), padding_ms / 1000, job["info"]["duration"])
        payload = json.dumps([ranges, mode])
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def run_render(job_id: str, mode: str, padding_ms: int) -> None:
        job = store.load(job_id)
        source = source_path(job)
        target = store.path(job_id) / f"clean{job['extension']}"
        ranges = pipeline.censor_ranges(job["flags"], padding_ms / 1000, job["info"]["duration"])
        signature = render_signature(job, mode, padding_ms)

        # Kept in memory so a long transcript isn't rewritten to disk during every update.
        def report(fraction: float) -> None:
            render_progress[job_id] = round(fraction, 4)

        try:
            pipeline.render(source, target, job["info"], ranges, mode, report)
            with store.lock:
                current = store.load(job_id)
                current["render"].update(status="done", progress=1.0, signature=signature,
                                         ranges=len(ranges), error=None, finished=time.time())
                store.save(current)
        except Exception as error:
            app.logger.exception("Render failed for %s", job_id)
            with store.lock:
                current = store.load(job_id)
                current["render"].update(status="failed", error=_friendly_error(error))
                store.save(current)
        finally:
            render_progress.pop(job_id, None)

    # ------------------------------------------------------------------ pages

    @app.before_request
    def block_cross_site_writes():
        # Keeps other websites from changing this app through your browser.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403)

    @app.get("/")
    def home():
        jobs = [_summary(job) for job in store.all()]
        return render_template("home.html", jobs=jobs, models=MODELS, default_model=DEFAULT_MODEL,
                               extensions=sorted(ALLOWED_EXTENSIONS))

    @app.post("/upload")
    def upload():
        file = request.files.get("video")
        if file is None or not file.filename:
            return jsonify(error="Choose a video to upload."), 400
        extension = Path(file.filename).suffix.lower()
        if extension not in ALLOWED_EXTENSIONS:
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            return jsonify(error=f"This file type isn't supported. Use one of these: {allowed}."), 400
        model = request.form.get("model", DEFAULT_MODEL)
        if model not in MODELS:
            return jsonify(error="Choose a transcription quality from the list."), 400
        job_id = uuid.uuid4().hex
        folder = store.path(job_id)
        folder.mkdir(parents=True)
        upload_id = request.args.get("upload_id", "")
        file.stream.seek(0, os.SEEK_END)
        total = file.stream.tell()
        file.stream.seek(0)
        saved = 0
        try:
            with (folder / f"source{extension}").open("wb") as output:
                while True:
                    block = file.stream.read(4 * 1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    saved += len(block)
                    if upload_id:
                        save_progress[upload_id] = {"saved": saved, "total": total}
        finally:
            save_progress.pop(upload_id, None)
        job = {
            "id": job_id, "name": file.filename, "extension": extension, "model": model,
            "created": time.time(), "status": "queued", "stage": "Waiting to start", "progress": 0.0,
            "error": None, "info": None, "words": [], "flags": [], "language": "",
            "preview": None, "render": None, "steps": _new_steps(),
        }
        store.save(job)
        workers.submit(process, job_id)
        return jsonify(id=job_id, url=url_for("review", job_id=job_id))

    @app.get("/upload-progress/<upload_id>")
    def upload_progress(upload_id: str):
        return jsonify(save_progress.get(upload_id) or {"saved": 0, "total": 0})

    @app.get("/jobs/<job_id>")
    def review(job_id: str):
        job = store.load(job_id)
        return render_template("review.html", job=_summary(job))

    @app.post("/jobs/<job_id>/delete")
    def delete_job(job_id: str):
        job = store.load(job_id)
        if job["status"] in {"queued", "processing"} or (job.get("render") or {}).get("status") == "rendering":
            return jsonify(error="Wait for this video to finish processing before deleting it."), 409
        with store.lock:
            shutil.rmtree(store.path(job_id))
        return redirect(url_for("home"), code=303)

    @app.route("/wordlist", methods=["GET", "POST"])
    def wordlist():
        saved = False
        if request.method == "POST":
            wordlist_file.write_text(request.form.get("words", ""), encoding="utf-8")
            saved = True
        text = wordlist_file.read_text(encoding="utf-8")
        return render_template("wordlist.html", words=text, saved=saved,
                               count=len(pipeline.parse_wordlist(text)))

    # -------------------------------------------------------------------- API

    def with_live_render(job: dict) -> dict | None:
        render_state = job.get("render")
        if render_state and render_state.get("status") == "done":
            render_state["stale"] = render_signature(
                job, render_state["mode"], render_state["padding_ms"]) != render_state.get("signature")
        elif render_state and render_state.get("status") == "rendering":
            render_state["progress"] = render_progress.get(job["id"], render_state.get("progress", 0.0))
        return render_state

    @app.get("/api/jobs/<job_id>")
    def job_state(job_id: str):
        job = store.load(job_id)
        with_live_render(job)
        return jsonify(job)

    @app.get("/api/jobs/<job_id>/status")
    def job_status(job_id: str):
        """Progress without the transcript, which can be several megabytes."""
        job = store.load(job_id)
        return jsonify(status=job["status"], error=job.get("error"), steps=job.get("steps") or [],
                       render=with_live_render(job))

    @app.post("/api/jobs/<job_id>/retry")
    def retry(job_id: str):
        job = store.load(job_id)
        if job["status"] != "failed":
            return jsonify(error="Only a failed video can be retried."), 409
        store.update(job_id, status="queued", stage="Waiting to start", progress=0.0, error=None, steps=_new_steps())
        workers.submit(process, job_id)
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/flags")
    def add_flag(job_id: str):
        body = request.get_json(silent=True) or {}
        with store.lock:
            job = _ready(store, job_id)
            try:
                start, end = _clean_times(body.get("start"), body.get("end"), job)
            except ValueError as error:
                return jsonify(error=str(error)), 400
            text = str(body.get("text") or "Added by hand")[:200]
            flag = {
                "id": uuid.uuid4().hex[:12], "start": start, "end": end, "text": text,
                "reason": "Added by hand", "source": "manual",
                "context": str(body.get("context") or "")[:500], "decision": "censor",
            }
            job["flags"].append(flag)
            job["flags"].sort(key=lambda item: item["start"])
            store.save(job)
        return jsonify(flag), 201

    @app.patch("/api/jobs/<job_id>/flags/<flag_id>")
    def edit_flag(job_id: str, flag_id: str):
        body = request.get_json(silent=True) or {}
        with store.lock:
            job = _ready(store, job_id)
            flag = next((item for item in job["flags"] if item["id"] == flag_id), None)
            if flag is None:
                abort(404)
            if "decision" in body:
                if body["decision"] not in DECISIONS:
                    return jsonify(error="Choose Censor or Keep."), 400
                flag["decision"] = body["decision"]
            if "start" in body or "end" in body:
                try:
                    start, end = _clean_times(body.get("start", flag["start"]), body.get("end", flag["end"]), job)
                except ValueError as error:
                    return jsonify(error=str(error)), 400
                flag.update(start=start, end=end, timing_edited=True)
                job["flags"].sort(key=lambda item: item["start"])
            store.save(job)
        return jsonify(flag)

    @app.delete("/api/jobs/<job_id>/flags/<flag_id>")
    def remove_flag(job_id: str, flag_id: str):
        with store.lock:
            job = _ready(store, job_id)
            flag = next((item for item in job["flags"] if item["id"] == flag_id), None)
            if flag is None:
                abort(404)
            if flag["source"] != "manual":
                return jsonify(error="Mark this flag as Keep instead of removing it."), 400
            job["flags"].remove(flag)
            store.save(job)
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/censor-undecided")
    def censor_undecided(job_id: str):
        with store.lock:
            job = _ready(store, job_id)
            for flag in job["flags"]:
                if flag["decision"] == "undecided":
                    flag["decision"] = "censor"
            store.save(job)
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/censor-word")
    def censor_word(job_id: str):
        """Censor every use of a transcript word and return what's needed to undo it."""
        body = request.get_json(silent=True) or {}
        with store.lock:
            job = _ready(store, job_id)
            index = body.get("word_index")
            if type(index) is not int or not 0 <= index < len(job["words"]):
                return jsonify(error="Choose a word from the transcript."), 400
            try:
                result = pipeline.censor_word_everywhere(job["words"], job["flags"], index)
            except ValueError as error:
                return jsonify(error=str(error)), 400
            for flag in result["added"]:
                flag["id"] = uuid.uuid4().hex[:12]
            job["flags"].extend(result["added"])
            job["flags"].sort(key=lambda item: item["start"])
            store.save(job)
        return jsonify(word=result["word"], matches=result["matches"], flags=job["flags"],
                       undo={"changed": result["changed"], "added": [flag["id"] for flag in result["added"]]})

    @app.post("/api/jobs/<job_id>/keep-word")
    def keep_word(job_id: str):
        """Keep every flag for a transcript word and return what's needed to undo it."""
        body = request.get_json(silent=True) or {}
        with store.lock:
            job = _ready(store, job_id)
            index = body.get("word_index")
            if type(index) is not int or not 0 <= index < len(job["words"]):
                return jsonify(error="Choose a word from the transcript."), 400
            try:
                result = pipeline.keep_word_everywhere(job["words"], job["flags"], index)
            except ValueError as error:
                return jsonify(error=str(error)), 400
            store.save(job)
        return jsonify(word=result["word"], matches=result["matches"], flags=job["flags"],
                       still_censored=result["still_censored"],
                       undo={"changed": result["changed"], "added": []})

    @app.post("/api/jobs/<job_id>/censor-word/undo")
    def undo_censor_word(job_id: str):
        body = request.get_json(silent=True) or {}
        changed = body.get("changed") or {}
        added = set(body.get("added") or [])
        with store.lock:
            job = _ready(store, job_id)
            job["flags"] = [flag for flag in job["flags"]
                            if not (flag["id"] in added and flag["source"] == "manual")]
            for flag in job["flags"]:
                if changed.get(flag["id"]) in DECISIONS:
                    flag["decision"] = changed[flag["id"]]
            store.save(job)
        return jsonify(flags=job["flags"])

    @app.post("/api/jobs/<job_id>/rescan")
    def rescan(job_id: str):
        """Apply the current word list without losing any decision or manual flag."""
        with store.lock:
            job = _ready(store, job_id)
            preserved = [flag for flag in job["flags"]
                         if flag["source"] == "manual" or flag["decision"] != "undecided"
                         or flag.get("timing_edited")]
            found = pipeline.scan_words(job["words"], read_entries())
            for flag in found:
                if not any(flag["start"] < other["end"] and other["start"] < flag["end"] for other in preserved):
                    flag["id"] = uuid.uuid4().hex[:12]
                    preserved.append(flag)
            job["flags"] = sorted(preserved, key=lambda item: item["start"])
            store.save(job)
        return jsonify(flags=len(job["flags"]))

    @app.post("/api/jobs/<job_id>/render")
    def start_render(job_id: str):
        body = request.get_json(silent=True) or {}
        mode = body.get("mode", "mute")
        if mode not in {"mute", "bleep"}:
            return jsonify(error="Choose Mute or Bleep."), 400
        try:
            padding_ms = int(body.get("padding_ms", 100))
        except (TypeError, ValueError):
            return jsonify(error="Enter the padding as a number of milliseconds."), 400
        if not 0 <= padding_ms <= 1000:
            return jsonify(error="Use a padding from 0 to 1000 milliseconds."), 400
        with store.lock:
            job = _ready(store, job_id)
            if (job.get("render") or {}).get("status") == "rendering":
                return jsonify(error="A render is already running."), 409
            if not any(flag["decision"] == "censor" for flag in job["flags"]):
                return jsonify(error="Mark at least one flag as Censor before rendering."), 400
            job["render"] = {"status": "rendering", "progress": 0.0, "mode": mode,
                             "padding_ms": padding_ms, "error": None}
            store.save(job)
        workers.submit(run_render, job_id, mode, padding_ms)
        return jsonify(ok=True), 202

    # ------------------------------------------------------------------ files

    @app.get("/media/<job_id>")
    def media(job_id: str):
        job = store.load(job_id)
        folder = store.path(job_id)
        path = folder / job["preview"] if job.get("preview") else source_path(job)
        return send_file(path, conditional=True)

    @app.get("/jobs/<job_id>/download")
    def download(job_id: str):
        job = store.load(job_id)
        path = store.path(job_id) / f"clean{job['extension']}"
        if not path.exists() or (job.get("render") or {}).get("status") != "done":
            abort(404)
        name = secure_filename(Path(job["name"]).stem) or "video"
        return send_file(path, as_attachment=True, download_name=f"{name}_clean{job['extension']}", conditional=True)

    @app.get("/jobs/<job_id>/flags.csv")
    def export_csv(job_id: str):
        job = store.load(job_id)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Start", "End", "Start seconds", "End seconds", "Text", "Reason", "Decision", "Context"])
        for flag in job.get("flags", []):
            writer.writerow([
                _timecode(flag["start"]), _timecode(flag["end"]), f"{flag['start']:.3f}", f"{flag['end']:.3f}",
                _csv_text(flag["text"]), _csv_text(flag["reason"]), flag["decision"], _csv_text(flag["context"]),
            ])
        name = secure_filename(Path(job["name"]).stem) or "video"
        data = io.BytesIO(("﻿" + buffer.getvalue()).encode("utf-8"))
        return send_file(data, mimetype="text/csv", as_attachment=True, download_name=f"{name}_flags.csv")

    return app


def _new_steps() -> list[dict]:
    return [{"key": key, "label": label, "status": "pending", "progress": 0.0 if measured else None}
            for key, label, measured in STEPS]


def _stop_active_steps(job: dict) -> None:
    for step in job.get("steps") or []:
        if step["status"] == "active":
            step["status"] = "stopped"


def _ready(store: JobStore, job_id: str) -> dict:
    job = store.load(job_id)
    if job["status"] != "ready":
        abort(409)
    return job


def _clean_times(start, end, job: dict) -> tuple[float, float]:
    try:
        start, end = round(float(start), 3), round(float(end), 3)
    except (TypeError, ValueError):
        raise ValueError("Enter the start and end as numbers of seconds.") from None
    duration = job["info"]["duration"]
    if not (0 <= start < end):
        raise ValueError("The end time must be after the start time.")
    if duration > 0 and start >= duration:
        raise ValueError("The start time is after the end of the video.")
    return start, min(end, duration) if duration > 0 else end


def _timecode(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def _csv_text(value: str) -> str:
    # Stops spreadsheet apps from treating transcript text as a formula.
    return "'" + value if value[:1] in {"=", "+", "-", "@", "\t", "\r"} else value


def _friendly_error(error: Exception) -> str:
    if isinstance(error, pipeline.MediaError):
        return str(error)
    text = str(error)
    return f"Something went wrong while processing this video: {text[:600]}"


def _summary(job: dict) -> dict:
    flags = job.get("flags", [])
    return {
        "id": job["id"], "name": job["name"], "status": job["status"], "stage": job.get("stage", ""),
        "created": time.strftime("%b %-d, %Y, %-I:%M %p", time.localtime(job["created"]))
        if os.name != "nt" else time.strftime("%b %d, %Y, %I:%M %p", time.localtime(job["created"])),
        "flag_count": len(flags),
        "undecided": sum(1 for flag in flags if flag["decision"] == "undecided"),
    }


def _check_ffmpeg() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise SystemExit(f"Clean Cut needs {' and '.join(missing)}. Install FFmpeg and make sure it is on your PATH.")


if __name__ == "__main__":
    faulthandler.enable()
    _check_ffmpeg()
    port = int(os.environ.get("PORT", 5057))
    print(f"Clean Cut is running at http://127.0.0.1:{port}")
    create_app().run(host="127.0.0.1", port=port, threaded=True)
