"""Tests for the job progress shown by the web app."""
import io
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as app_module  # noqa: E402
import pipeline  # noqa: E402


def fake_words():
    return [{"text": "damn", "start": 1.0, "end": 1.3, "confidence": 0.9, "segment": 0}]


@pytest.fixture
def sample(tmp_path):
    path = tmp_path / "sample.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10:d=4",
                    "-f", "lavfi", "-i", "sine=f=300:r=48000:d=4", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)
    return path


def upload(client, path):
    with path.open("rb") as video:
        response = client.post("/upload?upload_id=" + "a" * 32,
                               data={"video": (io.BytesIO(video.read()), path.name), "model": "small.en"},
                               content_type="multipart/form-data")
    return response.get_json()["id"]


def wait_for(client, job_id, key="status", values=("ready", "failed")):
    for _ in range(200):
        state = client.get(f"/api/jobs/{job_id}/status").get_json()
        if state[key] in values:
            return state
        time.sleep(0.05)
    raise AssertionError("The job didn't finish.")


def test_every_step_finishes_and_status_omits_the_transcript(tmp_path, sample, monkeypatch):
    def transcribe(wav, model, device, compute, on_progress):
        on_progress(0.25)
        on_progress(1.0)
        return fake_words(), "en"

    monkeypatch.setattr(pipeline, "transcribe_audio", transcribe)
    client = app_module.create_app(data_dir=tmp_path / "data").test_client()
    job_id = upload(client, sample)
    state = wait_for(client, job_id)
    assert state["status"] == "ready" and "words" not in state
    assert [step["key"] for step in state["steps"]] == ["read", "audio", "model", "transcribe", "flags"]
    assert all(step["status"] == "done" for step in state["steps"])
    assert client.get(f"/api/jobs/{job_id}").get_json()["flags"][0]["text"] == "damn"


def test_failure_marks_the_running_step_as_stopped(tmp_path, sample, monkeypatch):
    def transcribe(wav, model, device, compute, on_progress):
        on_progress(0.25)
        time.sleep(0.6)
        on_progress(0.5)
        raise pipeline.MediaError("Test failure")

    monkeypatch.setattr(pipeline, "transcribe_audio", transcribe)
    client = app_module.create_app(data_dir=tmp_path / "data").test_client()
    state = wait_for(client, upload(client, sample))
    steps = {step["key"]: step for step in state["steps"]}
    assert state["status"] == "failed" and state["error"] == "Test failure"
    assert steps["model"]["status"] == "done" and steps["audio"]["status"] == "done"
    assert steps["transcribe"]["status"] == "stopped" and 0.25 <= steps["transcribe"]["progress"] <= 0.5
    assert steps["flags"]["status"] == "pending"


def test_restart_stops_old_step_and_retry_starts_fresh(tmp_path, sample, monkeypatch):
    monkeypatch.setattr(pipeline, "transcribe_audio", lambda *args: (fake_words(), "en"))
    data = tmp_path / "data"
    client = app_module.create_app(data_dir=data).test_client()
    job_id = upload(client, sample)
    wait_for(client, job_id)
    store = app_module.JobStore(data / "jobs")
    job = store.load(job_id)
    job["status"] = "processing"
    job["steps"][3].update(status="active", progress=0.4)
    store.save(job)
    restarted = app_module.create_app(data_dir=data).test_client()
    state = restarted.get(f"/api/jobs/{job_id}/status").get_json()
    assert state["status"] == "failed" and state["steps"][3]["status"] == "stopped"
    assert restarted.post(f"/api/jobs/{job_id}/retry").status_code == 200
    assert wait_for(restarted, job_id)["status"] == "ready"


def test_job_from_before_steps_existed_still_reports_status(tmp_path, sample, monkeypatch):
    monkeypatch.setattr(pipeline, "transcribe_audio", lambda *args: (fake_words(), "en"))
    data = tmp_path / "data"
    client = app_module.create_app(data_dir=data).test_client()
    job_id = upload(client, sample)
    wait_for(client, job_id)
    store = app_module.JobStore(data / "jobs")
    job = store.load(job_id)
    del job["steps"]
    store.save(job)
    state = client.get(f"/api/jobs/{job_id}/status").get_json()
    assert state["status"] == "ready" and state["steps"] == []


def test_render_progress_is_reported_then_finished(tmp_path, sample, monkeypatch):
    monkeypatch.setattr(pipeline, "transcribe_audio", lambda *args: (fake_words(), "en"))
    client = app_module.create_app(data_dir=tmp_path / "data").test_client()
    job_id = upload(client, sample)
    wait_for(client, job_id)
    flag = client.get(f"/api/jobs/{job_id}").get_json()["flags"][0]
    client.patch(f"/api/jobs/{job_id}/flags/{flag['id']}", json={"decision": "censor"})
    assert client.post(f"/api/jobs/{job_id}/render", json={"mode": "mute", "padding_ms": 100}).status_code == 202
    for _ in range(200):
        render_state = client.get(f"/api/jobs/{job_id}/status").get_json()["render"]
        if render_state["status"] != "rendering":
            break
        time.sleep(0.05)
    assert render_state["status"] == "done" and render_state["progress"] == 1.0


def test_upload_progress_for_unknown_id_is_empty(tmp_path):
    client = app_module.create_app(data_dir=tmp_path / "data").test_client()
    assert client.get("/upload-progress/" + "b" * 32).get_json() == {"saved": 0, "total": 0}
