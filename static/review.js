(() => {
  const jobId = document.getElementById("app").dataset.job;
  const $ = (id) => document.getElementById(id);
  const player = $("player");

  let job = null;
  let reviewReady = false;
  let selectedId = null;
  let filter = "all";
  let clipEnd = null;
  let wordEls = [];
  let wordRange = null;
  let anchorWord = null;
  let nowWord = -1;
  let silencing = false;
  let mutedBeforeSilence = false;

  const timecode = (seconds) => {
    const total = Math.max(seconds, 0);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    const clock = `${String(minutes).padStart(2, "0")}:${secs.toFixed(2).padStart(5, "0")}`;
    return hours ? `${hours}:${clock}` : clock;
  };

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const say = (text, isError = false, action = null) => {
    const box = $("status");
    box.textContent = text || "";
    box.hidden = !text;
    box.classList.toggle("error", isError);
    if (action) {
      const button = element("button", "quiet status-action", action.label);
      button.type = "button";
      button.addEventListener("click", action.run);
      box.append(" ", button);
    }
  };

  // Mirrors pipeline.normalize so the menu can show a count before anything changes.
  const normalizeWord = (text) => text.normalize("NFKC").toLowerCase().replace(/\u2019/g, "'")
    .replace(/^[^\p{L}\p{N}_*]+|[^\p{L}\p{N}_*]+$/gu, "");
  const instancesOf = (index) => {
    const target = normalizeWord(job.words[index].text);
    return target ? job.words.filter((word) => normalizeWord(word.text) === target).length : 0;
  };
  const allInstancesLabel = (index, verb) => {
    const count = instancesOf(index);
    return count === 1
      ? `${verb} this word, “${normalizeWord(job.words[index].text)}”`
      : `${verb} all ${count} instances of “${normalizeWord(job.words[index].text)}”`;
  };

  const api = async (method, path, body) => {
    const response = await fetch(`/api/jobs/${jobId}${path}`, {
      method,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "That change didn't save. Check that Clean Cut is still running.");
    return data;
  };

  const WORD_HINT = "Click a word to jump to it, shift-click to select several, or right-click to censor or keep every instance.";

  const getPadding = () => {
    const value = Number($("padding").value);
    return Number.isFinite(value) ? Math.min(Math.max(value, 0), 1000) / 1000 : 0.1;
  };
  const flagById = (id) => job.flags.find((flag) => flag.id === id);
  const visibleFlags = () => job.flags.filter((flag) => filter === "all" || flag.decision === filter);

  // ---------------------------------------------------------------- loading

  async function poll() {
    let state;
    try {
      state = await api("GET", "/status");
    } catch (error) {
      if (reviewReady) say(error.message, true);
      setTimeout(poll, 2000);
      return;
    }
    if (state.status === "ready") {
      if (!reviewReady) {
        try {
          job = await api("GET", "");
        } catch {
          setTimeout(poll, 2000);
          return;
        }
        startReview();
      }
      job.render = state.render;
      renderRenderPanel();
      if (state.render?.status === "rendering") setTimeout(poll, 1000);
      return;
    }
    showProcessing(state);
    if (state.status !== "failed") setTimeout(poll, 1000);
  }

  function stepText(step) {
    const percent = step.progress === null ? null : Math.floor(step.progress * 100);
    if (step.status === "done") return "100%";
    if (step.status === "stopped") return percent === null ? "Stopped" : `Stopped at ${percent}%`;
    if (step.status === "active") return percent === null ? "In progress" : `${percent}%`;
    return percent === null ? "Waiting" : "0%";
  }

  function renderSteps(steps) {
    const list = $("steps");
    const keys = steps.map((step) => step.key).join();
    if (list.dataset.keys !== keys) {
      list.dataset.keys = keys;
      list.replaceChildren(...steps.map((step) => {
        const row = element("li", "step");
        row.dataset.key = step.key;
        const head = element("div", "step-head");
        head.append(element("span", "", step.label), element("span", "step-value"));
        const meter = element("div", "meter");
        meter.setAttribute("role", "progressbar");
        meter.setAttribute("aria-label", step.label);
        meter.setAttribute("aria-valuemin", "0");
        meter.setAttribute("aria-valuemax", "100");
        meter.append(element("span"));
        row.append(head, meter);
        return row;
      }));
    }
    steps.forEach((step) => {
      const row = list.querySelector(`[data-key="${step.key}"]`);
      const measured = step.progress !== null;
      const percent = step.status === "done" ? 100 : Math.floor((step.progress || 0) * 100);
      row.className = `step step-${step.status}${!measured && step.status === "active" ? " step-unmeasured" : ""}`;
      row.querySelector(".step-value").textContent = stepText(step);
      const meter = row.querySelector(".meter");
      meter.querySelector("span").style.width = measured || step.status === "done" ? `${percent}%` : "";
      if (measured || step.status === "done") meter.setAttribute("aria-valuenow", String(percent));
      else meter.removeAttribute("aria-valuenow");
    });
  }

  function showProcessing(state) {
    $("processing").hidden = false;
    $("review").hidden = true;
    const failed = state.status === "failed";
    $("processing-title").textContent = failed ? "This video couldn't be processed" : "Working on your video";
    renderSteps(state.steps);
    $("processing-error").hidden = !failed;
    $("processing-error").textContent = state.error || "";
    $("retry").hidden = !failed;
  }

  function startReview() {
    reviewReady = true;
    $("processing").hidden = true;
    $("review").hidden = false;
    player.src = `/media/${jobId}`;
    const notes = [];
    if (job.preview) notes.push("You're reviewing a smaller preview. The render uses your original video.");
    if (job.info?.has_subtitles) notes.push("This video has a subtitle track. Rendering won't change it, so censored words can still appear in the subtitles.");
    $("source-note").textContent = notes.join(" ");
    $("source-note").hidden = !notes.length;
    buildTranscript();
    refresh();
    requestAnimationFrame(tick);
  }

  // ------------------------------------------------------------- transcript

  function buildTranscript() {
    const box = $("transcript");
    box.replaceChildren();
    wordEls = [];
    if (!job.words.length) {
      box.append(element("p", "empty", "Whisper found no speech in this video. Listen through it and add flags with Add flag at playhead."));
      return;
    }
    let paragraph = null;
    let segment = null;
    job.words.forEach((word, index) => {
      if (word.segment !== segment) {
        segment = word.segment;
        paragraph = element("p", "line");
        const stamp = element("button", "stamp", timecode(word.start).replace(/\.\d+$/, ""));
        stamp.type = "button";
        stamp.dataset.time = word.start;
        stamp.setAttribute("aria-label", `Jump to ${timecode(word.start)}`);
        paragraph.append(stamp);
        box.append(paragraph);
      }
      const span = element("span", "word", word.text);
      span.dataset.index = index;
      paragraph.append(span, " ");
      wordEls.push(span);
    });
  }

  function paintWords() {
    job.words.forEach((word, index) => {
      const middle = (word.start + word.end) / 2;
      const covering = job.flags.filter((item) => middle >= item.start && middle <= item.end);
      const flag = covering.find((item) => item.decision === "censor") || covering[0];
      const classes = ["word"];
      if (flag) classes.push(`flag-${flag.decision}`);
      if (flag && flag.id === selectedId) classes.push("selected-flag");
      if (word.confidence < 0.5) classes.push("unsure");
      if (wordRange && index >= wordRange[0] && index <= wordRange[1]) classes.push("picked");
      if (index === nowWord) classes.push("now");
      wordEls[index].className = classes.join(" ");
    });
    const label = $("word-selection");
    if (wordRange) {
      const words = job.words.slice(wordRange[0], wordRange[1] + 1);
      label.textContent = `“${words.map((word) => word.text).join(" ")}”`;
    } else {
      label.textContent = WORD_HINT;
    }
    $("flag-words").hidden = !wordRange;
    const single = wordRange && wordRange[0] === wordRange[1] && instancesOf(wordRange[0]) > 0;
    $("censor-word").hidden = !single;
    $("keep-word").hidden = !single;
    if (single) {
      $("censor-word").textContent = allInstancesLabel(wordRange[0], "Censor");
      $("keep-word").textContent = allInstancesLabel(wordRange[0], "Keep");
    }
  }

  // ------------------------------------------------------------------ flags

  function renderFilters() {
    const counts = { all: job.flags.length, undecided: 0, censor: 0, keep: 0 };
    job.flags.forEach((flag) => { counts[flag.decision] += 1; });
    const labels = { all: "All", undecided: "Undecided", censor: "Censor", keep: "Keep" };
    const box = $("filters");
    box.replaceChildren();
    Object.keys(labels).forEach((key) => {
      const button = element("button", "filter", `${labels[key]} ${counts[key]}`);
      button.type = "button";
      button.setAttribute("aria-pressed", String(filter === key));
      button.addEventListener("click", () => { filter = key; refresh(); });
      box.append(button);
    });
    $("censor-all").hidden = !counts.undecided;
  }

  function renderFlags() {
    const list = $("flag-list");
    list.replaceChildren();
    const flags = visibleFlags();
    flags.forEach((flag) => list.append(flagRow(flag)));
    const empty = $("flags-empty");
    empty.hidden = flags.length > 0;
    if (!job.flags.length) {
      empty.textContent = "Nothing on your word list was found. Whisper can miss or mishear words, so listen through the video if it's going to a strict audience. Use the transcript or Add flag at playhead to flag anything it missed.";
    } else if (!flags.length) {
      empty.textContent = "No flags match this filter.";
    }
  }

  function flagRow(flag) {
    const row = element("li", `flag decision-${flag.decision}`);
    row.dataset.id = flag.id;
    if (flag.id === selectedId) row.classList.add("selected");

    const main = element("button", "flag-main");
    main.type = "button";
    main.append(
      element("span", "flag-time", timecode(flag.start)),
      element("span", "flag-text", flag.text),
    );
    if (flag.reason !== flag.text) main.append(element("span", "flag-reason", flag.reason));
    if (flag.context) main.append(element("span", "flag-context", flag.context));
    main.addEventListener("click", () => selectFlag(flag.id, true));

    const decisions = element("div", "flag-decisions");
    decisions.append(decisionButton(flag, "censor", "Censor"), decisionButton(flag, "keep", "Keep"));
    row.append(main, decisions);
    if (flag.id === selectedId) row.append(timingEditor(flag));
    return row;
  }

  function decisionButton(flag, decision, label) {
    const button = element("button", `decide decide-${decision}`, label);
    button.type = "button";
    button.setAttribute("aria-pressed", String(flag.decision === decision));
    button.setAttribute("aria-label", `${label} ${flag.text} at ${timecode(flag.start)}`);
    button.addEventListener("click", () => decide(flag.id, flag.decision === decision ? "undecided" : decision));
    return button;
  }

  function timingEditor(flag) {
    const box = element("div", "timing");
    const field = (name, label, value) => {
      const wrap = element("label", "field", undefined);
      wrap.append(element("span", "", label));
      const input = document.createElement("input");
      input.type = "number";
      input.step = "0.01";
      input.min = "0";
      input.value = value.toFixed(3);
      input.addEventListener("change", () => saveTiming(flag.id, name, input));
      wrap.append(input);
      return wrap;
    };
    box.append(field("start", "Start (seconds)", flag.start), field("end", "End (seconds)", flag.end));
    const replay = element("button", "secondary", "Replay");
    replay.type = "button";
    replay.addEventListener("click", () => playFlag(flagById(flag.id)));
    box.append(replay);
    if (flag.source === "manual") {
      const remove = element("button", "quiet", "Remove flag");
      remove.type = "button";
      remove.addEventListener("click", () => removeFlag(flag.id));
      box.append(remove);
    }
    return box;
  }

  async function saveTiming(id, name, input) {
    try {
      const updated = await api("PATCH", `/flags/${id}`, { [name]: Number(input.value) });
      Object.assign(flagById(id), updated);
      job.flags.sort((a, b) => a.start - b.start);
      say("");
      refresh();
    } catch (error) {
      input.value = flagById(id)[name].toFixed(3);
      say(error.message, true);
    }
  }

  async function decide(id, decision) {
    const flag = flagById(id);
    const previous = flag.decision;
    flag.decision = decision;
    const nextId = decision === "undecided" ? id : nextUndecidedAfter(id);
    selectedId = nextId || id;
    refresh();
    focusSelected();
    if (nextId && nextId !== id) playFlag(flagById(nextId));
    try {
      await api("PATCH", `/flags/${id}`, { decision });
      say("");
    } catch (error) {
      flag.decision = previous;
      refresh();
      say(error.message, true);
    }
  }

  function nextUndecidedAfter(id) {
    const index = job.flags.findIndex((flag) => flag.id === id);
    const after = job.flags.slice(index + 1).concat(job.flags.slice(0, index));
    return after.find((flag) => flag.decision === "undecided")?.id || null;
  }

  async function removeFlag(id) {
    try {
      await api("DELETE", `/flags/${id}`);
      job.flags = job.flags.filter((flag) => flag.id !== id);
      selectedId = null;
      refresh();
    } catch (error) {
      say(error.message, true);
    }
  }

  async function addFlag(start, end, text, context) {
    try {
      const flag = await api("POST", "/flags", { start, end, text, context });
      job.flags.push(flag);
      job.flags.sort((a, b) => a.start - b.start);
      selectedId = flag.id;
      filter = "all";
      wordRange = null;
      say("");
      refresh();
      focusSelected();
    } catch (error) {
      say(error.message, true);
    }
  }

  function selectFlag(id, play) {
    selectedId = id;
    refresh();
    focusSelected();
    if (play) playFlag(flagById(id));
  }

  function focusSelected() {
    const row = document.querySelector(`.flag[data-id="${selectedId}"]`);
    if (row) {
      row.querySelector(".flag-main").focus({ preventScroll: true });
      row.scrollIntoView({ block: "nearest" });
    }
    const word = job.words.findIndex((item) => {
      const flag = flagById(selectedId);
      return flag && (item.start + item.end) / 2 >= flag.start;
    });
    if (word >= 0 && wordEls[word]) {
      const box = $("transcript");
      const top = wordEls[word].offsetTop - box.offsetTop;
      if (top < box.scrollTop || top > box.scrollTop + box.clientHeight - 40) {
        box.scrollTop = top - box.clientHeight / 2;
      }
    }
  }

  function playFlag(flag) {
    if (!flag) return;
    player.currentTime = Math.max(flag.start - 1.5, 0);
    clipEnd = flag.end + 1.5;
    player.play().catch(() => { /* The browser may require a click on the video first. */ });
  }

  function moveSelection(direction) {
    const flags = visibleFlags();
    if (!flags.length) return;
    const index = flags.findIndex((flag) => flag.id === selectedId);
    const next = flags[Math.min(Math.max(index + direction, 0), flags.length - 1)];
    selectFlag(next.id, true);
  }

  // --------------------------------------------------------------- timeline

  function renderTimeline() {
    const line = $("timeline");
    line.querySelectorAll(".mark").forEach((mark) => mark.remove());
    const duration = job.info.duration || 1;
    job.flags.forEach((flag) => {
      const mark = element("button", `mark mark-${flag.decision}`);
      mark.type = "button";
      mark.tabIndex = -1;
      mark.setAttribute("aria-hidden", "true");
      mark.title = `${timecode(flag.start)} ${flag.text}`;
      mark.style.left = `${(flag.start / duration) * 100}%`;
      mark.style.width = `${((flag.end - flag.start) / duration) * 100}%`;
      if (flag.id === selectedId) mark.classList.add("selected");
      mark.addEventListener("click", (event) => { event.stopPropagation(); selectFlag(flag.id, true); });
      line.append(mark);
    });
  }

  function findWordAt(time) {
    let low = 0;
    let high = job.words.length - 1;
    while (low <= high) {
      const middle = (low + high) >> 1;
      const word = job.words[middle];
      if (time < word.start) high = middle - 1;
      else if (time > word.end) low = middle + 1;
      else return middle;
    }
    return -1;
  }

  function tick() {
    const time = player.currentTime;
    const duration = job.info.duration || player.duration || 1;
    $("playhead").style.left = `${Math.min((time / duration) * 100, 100)}%`;

    const padding = getPadding();
    const shouldSilence = $("preview-toggle").checked && job.flags.some(
      (flag) => flag.decision === "censor" && time >= flag.start - padding && time <= flag.end + padding,
    );
    if (shouldSilence && !silencing) {
      mutedBeforeSilence = player.muted;
      player.muted = true;
      silencing = true;
    } else if (!shouldSilence && silencing) {
      player.muted = mutedBeforeSilence;
      silencing = false;
    }

    if (clipEnd !== null && time >= clipEnd) {
      player.pause();
      clipEnd = null;
    }

    const current = findWordAt(time);
    if (current !== nowWord) {
      if (nowWord >= 0 && wordEls[nowWord]) wordEls[nowWord].classList.remove("now");
      if (current >= 0 && wordEls[current]) wordEls[current].classList.add("now");
      nowWord = current;
    }
    requestAnimationFrame(tick);
  }

  // ----------------------------------------------------------------- render

  function renderRenderPanel() {
    const censor = job.flags.filter((flag) => flag.decision === "censor").length;
    const undecided = job.flags.filter((flag) => flag.decision === "undecided").length;
    const flagWord = (count) => `${count} flag${count === 1 ? "" : "s"}`;
    let summary = censor ? `${flagWord(censor)} will be censored.` : "Mark at least one flag as Censor to render.";
    if (undecided) summary += ` ${flagWord(undecided)} still undecided will stay audible.`;
    $("render-summary").textContent = summary;

    const state = job.render;
    const rendering = state?.status === "rendering";
    $("render-button").disabled = rendering || !censor;
    $("render-button").textContent = rendering ? "Rendering" : "Render clean video";
    $("render-progress").hidden = !rendering;
    if (rendering) {
      const percent = Math.floor((state.progress || 0) * 100);
      $("render-progress-label").textContent = percent >= 99 ? "Finishing the file" : "Rendering";
      $("render-progress-value").textContent = `${percent}%`;
      $("render-meter").firstElementChild.style.width = `${percent}%`;
      $("render-meter").setAttribute("aria-valuenow", String(percent));
    }

    const result = $("render-result");
    result.replaceChildren();
    result.hidden = !state || rendering;
    result.classList.toggle("error", state?.status === "failed");
    if (state?.status === "failed") {
      result.textContent = state.error;
    } else if (state?.status === "done") {
      const link = element("a", "download", "Download clean video");
      link.href = `/jobs/${jobId}/download`;
      result.append(link);
      if (state.stale) result.append(element("span", "stale", " You changed flags or settings after this render. Render again to include them."));
    }
  }

  function refresh() {
    renderFilters();
    renderFlags();
    renderTimeline();
    paintWords();
    renderRenderPanel();
    refreshStale();
  }

  let staleTimer = null;
  function refreshStale() {
    if (!job.render || job.render.status !== "done") return;
    clearTimeout(staleTimer);
    staleTimer = setTimeout(async () => {
      try {
        const fresh = await api("GET", "/status");
        job.render = fresh.render;
        renderRenderPanel();
      } catch { /* The next action will report any connection problem. */ }
    }, 300);
  }

  // ------------------------------------------------------- censor all menu

  const menu = $("word-menu");
  const menuCensor = $("word-menu-censor");
  const menuKeep = $("word-menu-keep");
  const menuItems = [menuCensor, menuKeep];
  let menuWord = null;
  let menuReturnFocus = null;

  function closeWordMenu() {
    if (menu.hidden) return;
    menu.hidden = true;
    menuWord = null;
    menuReturnFocus?.focus({ preventScroll: true });
    menuReturnFocus = null;
  }

  function openWordMenu(index, x, y) {
    menuWord = index;
    menuCensor.textContent = allInstancesLabel(index, "Censor");
    menuKeep.textContent = allInstancesLabel(index, "Keep");
    menu.hidden = false;
    const box = menu.getBoundingClientRect();
    menu.style.left = `${Math.max(Math.min(x, window.innerWidth - box.width - 8), 8)}px`;
    menu.style.top = `${Math.max(Math.min(y, window.innerHeight - box.height - 8), 8)}px`;
    menuReturnFocus = $("transcript");
    menuCensor.focus({ preventScroll: true });
  }

  async function censorEveryInstance(index) {
    try {
      const result = await api("POST", "/censor-word", { word_index: index });
      job.flags = result.flags;
      wordRange = null;
      refresh();
      const count = result.matches === 1 ? "1 instance" : `${result.matches} instances`;
      say(`Censored ${count} of “${result.word}”.`, false, {
        label: "Undo",
        run: () => undoCensor(result.word, result.undo),
      });
    } catch (error) {
      say(error.message, true);
    }
  }

  async function keepEveryInstance(index) {
    try {
      const result = await api("POST", "/keep-word", { word_index: index });
      job.flags = result.flags;
      wordRange = null;
      refresh();
      const count = result.matches === 1 ? "1 instance" : `${result.matches} instances`;
      let message = `Kept ${count} of “${result.word}”.`;
      if (result.still_censored) {
        const stay = result.still_censored === 1 ? "1 is" : `${result.still_censored} are`;
        message += ` ${stay} still censored as part of a longer flag.`;
      }
      say(message, false, { label: "Undo", run: () => undoCensor(result.word, result.undo) });
    } catch (error) {
      say(error.message, true);
    }
  }

  async function undoCensor(word, undo) {
    try {
      const result = await api("POST", "/censor-word/undo", undo);
      job.flags = result.flags;
      if (selectedId && !flagById(selectedId)) selectedId = null;
      refresh();
      say(`Restored the previous flags for “${word}”.`);
    } catch (error) {
      say(error.message, true);
    }
  }

  $("transcript").addEventListener("contextmenu", (event) => {
    const span = event.target.closest(".word");
    if (!span) return;
    const index = Number(span.dataset.index);
    if (!instancesOf(index)) return;
    event.preventDefault();
    openWordMenu(index, event.clientX, event.clientY);
  });
  menuCensor.addEventListener("click", () => {
    const index = menuWord;
    closeWordMenu();
    if (index !== null) censorEveryInstance(index);
  });
  menuKeep.addEventListener("click", () => {
    const index = menuWord;
    closeWordMenu();
    if (index !== null) keepEveryInstance(index);
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const next = menuItems[menuItems.indexOf(document.activeElement) === 0 ? 1 : 0];
    next.focus({ preventScroll: true });
  });
  document.addEventListener("pointerdown", (event) => {
    if (!menu.contains(event.target)) closeWordMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeWordMenu();
  });
  window.addEventListener("resize", closeWordMenu);
  document.addEventListener("scroll", closeWordMenu, true);

  $("censor-word").addEventListener("click", () => {
    if (wordRange) censorEveryInstance(wordRange[0]);
  });
  $("keep-word").addEventListener("click", () => {
    if (wordRange) keepEveryInstance(wordRange[0]);
  });

  // ----------------------------------------------------------------- events

  $("retry").addEventListener("click", async () => {
    try {
      await api("POST", "/retry");
      poll();
    } catch (error) {
      $("processing-error").textContent = error.message;
    }
  });

  $("timeline").addEventListener("click", (event) => {
    const box = event.currentTarget.getBoundingClientRect();
    clipEnd = null;
    player.currentTime = ((event.clientX - box.left) / box.width) * (job.info.duration || player.duration || 0);
  });

  $("transcript").addEventListener("click", (event) => {
    const stamp = event.target.closest(".stamp");
    if (stamp) {
      clipEnd = null;
      player.currentTime = Number(stamp.dataset.time);
      return;
    }
    const span = event.target.closest(".word");
    if (!span) return;
    const index = Number(span.dataset.index);
    if (event.shiftKey && anchorWord !== null) {
      wordRange = [Math.min(anchorWord, index), Math.max(anchorWord, index)];
      window.getSelection()?.removeAllRanges();
    } else {
      anchorWord = index;
      wordRange = [index, index];
      clipEnd = null;
      player.currentTime = job.words[index].start;
    }
    paintWords();
  });

  $("flag-words").addEventListener("click", () => {
    const [first, last] = wordRange;
    const words = job.words.slice(first, last + 1);
    const before = job.words.slice(Math.max(first - 7, 0), first).map((word) => word.text);
    const after = job.words.slice(last + 1, last + 8).map((word) => word.text);
    addFlag(words[0].start, words[words.length - 1].end, words.map((word) => word.text).join(" "),
      [...before, ...words.map((word) => word.text), ...after].join(" "));
  });

  $("add-at-playhead").addEventListener("click", () => {
    const start = player.currentTime;
    addFlag(start, start + 0.5, "Added by hand", "");
  });

  $("censor-all").addEventListener("click", async () => {
    try {
      await api("POST", "/censor-undecided");
      job.flags.forEach((flag) => { if (flag.decision === "undecided") flag.decision = "censor"; });
      refresh();
    } catch (error) {
      say(error.message, true);
    }
  });

  $("rescan").addEventListener("click", async () => {
    try {
      const result = await api("POST", "/rescan");
      job = await api("GET", "");
      say(`Scan finished with the saved word list. There are now ${result.flags} flag${result.flags === 1 ? "" : "s"}.`);
      refresh();
    } catch (error) {
      say(error.message, true);
    }
  });

  $("padding").addEventListener("change", refreshStale);

  $("render-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const mode = new FormData(event.currentTarget).get("mode");
    try {
      await api("POST", "/render", { mode, padding_ms: Number($("padding").value) });
      say("");
      poll();
    } catch (error) {
      say(error.message, true);
    }
  });

  document.addEventListener("keydown", (event) => {
    if (!reviewReady || !menu.hidden || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.target.closest("input, select, textarea, video")) return;
    const key = event.key.toLowerCase();
    if (key === "arrowdown") { event.preventDefault(); moveSelection(1); }
    else if (key === "arrowup") { event.preventDefault(); moveSelection(-1); }
    else if (selectedId && key === "c") decide(selectedId, "censor");
    else if (selectedId && key === "k") decide(selectedId, "keep");
    else if (selectedId && key === "r") playFlag(flagById(selectedId));
  });

  poll();
})();
