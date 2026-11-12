(() => {
  const form = document.getElementById("upload-form");
  const input = document.getElementById("video");
  const zone = document.getElementById("dropzone");
  const title = document.getElementById("drop-title");
  const note = document.getElementById("drop-note");
  const button = document.getElementById("upload-button");
  const steps = document.getElementById("upload-steps");
  let saveTimer = null;

  const setStep = (id, fraction, active) => {
    const step = document.getElementById(id);
    const percent = Math.min(Math.floor(fraction * 100), 100);
    step.classList.toggle("step-active", active);
    step.classList.toggle("step-done", percent >= 100 && !active);
    step.querySelector(".step-value").textContent = `${percent}%`;
    step.querySelector(".meter span").style.width = `${percent}%`;
    step.querySelector(".meter").setAttribute("aria-valuenow", String(percent));
  };
  const watchSave = (uploadId) => {
    if (saveTimer) return;
    setStep("step-upload", 1, false);
    setStep("step-save", 0, true);
    saveTimer = setInterval(async () => {
      try {
        const response = await fetch(`/upload-progress/${uploadId}`);
        const state = await response.json();
        if (state.total) setStep("step-save", state.saved / state.total, true);
      } catch { /* The upload's own result reports any connection problem. */ }
    }, 500);
  };
  const stopSaveWatch = () => {
    clearInterval(saveTimer);
    saveTimer = null;
  };
  const errorBox = document.getElementById("upload-error");

  const showFile = () => {
    if (input.files.length) {
      title.textContent = input.files[0].name;
      note.textContent = `${(input.files[0].size / 1048576).toFixed(1)} MB`;
    }
  };
  const showError = (text) => {
    errorBox.textContent = text;
    errorBox.hidden = false;
    button.disabled = false;
    button.textContent = "Upload and transcribe";
    stopSaveWatch();
    steps.hidden = true;
  };

  input.addEventListener("change", showFile);
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => {
    event.preventDefault();
    zone.classList.remove("dragging");
  }));
  zone.addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length) {
      input.files = event.dataTransfer.files;
      showFile();
    }
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    const request = new XMLHttpRequest();
    const data = new FormData(form);
    button.disabled = true;
    button.textContent = "Uploading";
    steps.hidden = false;
    setStep("step-upload", 0, true);
    setStep("step-save", 0, false);
    const uploadId = Array.from(crypto.getRandomValues(new Uint8Array(16)),
      (value) => value.toString(16).padStart(2, "0")).join("");
    request.upload.addEventListener("progress", (progress) => {
      if (progress.lengthComputable) {
        // Stay below 100% until the browser confirms every byte was sent.
        setStep("step-upload", Math.min(progress.loaded / progress.total, 0.99), true);
      }
    });
    request.upload.addEventListener("load", () => watchSave(uploadId));
    request.addEventListener("load", () => {
      let body = {};
      try { body = JSON.parse(request.responseText); } catch { /* handled below */ }
      if (request.status === 200 && body.url) {
        stopSaveWatch();
        setStep("step-upload", 1, false);
        setStep("step-save", 1, false);
        window.location.href = body.url;
      } else {
        showError(body.error || "The upload failed. Check the file and try again.");
      }
    });
    request.addEventListener("error", () => showError("The upload stopped. Check that Clean Cut is still running, then try again."));
    request.open("POST", `/upload?upload_id=${uploadId}`);
    request.send(data);
  });
})();
