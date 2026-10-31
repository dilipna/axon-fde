import { getStory, readStudyLocation, studyHash } from "./story.mjs";
import {
  verifyBundle,
  rankPlanning,
  answerRecorded,
  planningKey,
  makePacket,
  makeBrief,
} from "./workbench-core.mjs";
const $ = (id) => document.getElementById(id);
const labels = (id) =>
  id.replaceAll("_", " ").replace(/^./, (s) => s.toUpperCase());
const money = (n) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n);
let data,
  manifest,
  caseId = "compressor",
  reviewKey = null,
  timer = null,
  planningValid = false,
  chapter = 0;
const reduced = matchMedia("(prefers-reduced-motion: reduce)");
function el(tag, text, className) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (className) n.className = className;
  return n;
}
function tell(text) {
  $("notice").textContent = text;
}
function points() {
  return data.telemetry[caseId].points;
}
function planning() {
  const blocked = [];
  if ($("facility-unavailable").checked)
    blocked.push("reroute_to_cold_storage");
  if ($("trailer-unavailable").checked) blocked.push("trailer_swap");
  return {
    probability: Number($("planning-risk").value) / 100,
    value:
      $("planning-value").value === ""
        ? NaN
        : Number($("planning-value").value),
    blocked,
  };
}
function stop() {
  clearInterval(timer);
  timer = null;
  $("play").textContent = "Play recorded timeline";
}
function svg(tag, attributes, text) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes))
    n.setAttribute(key, String(value));
  if (text !== undefined) n.textContent = text;
  return n;
}
function renderSignal() {
  const ps = points(),
    i = Math.min(Number($("timeline").value), ps.length - 1),
    p = ps[i];
  $("minute-value").textContent = String(p.minute);
  $("timeline-output").textContent = String(p.minute);
  $("temperature-value").textContent = p.cargo_temp_c.toFixed(2) + " °C";
  $("ambient-value").textContent = Number.isFinite(p.ambient_temp_c)
    ? p.ambient_temp_c.toFixed(1) + " °C"
    : "Not recorded";
  const chart = $("telemetry-chart");
  chart.replaceChildren(
    svg("title", { id: "chart-title" }, "Recorded cargo temperature"),
    svg(
      "desc",
      { id: "chart-description" },
      "Saved observations, not a forecast or live tracking.",
    ),
  );
  const lo = Math.min(2, ...ps.map((p) => p.cargo_temp_c)) - 1,
    hi = Math.max(8, ...ps.map((p) => p.cargo_temp_c)) + 1;
  const x = (j) => 48 + (j / Math.max(ps.length - 1, 1)) * 720,
    y = (t) => 210 - ((t - lo) / (hi - lo)) * 185;
  for (
    let t = Math.ceil(lo);
    t < hi;
    t += Math.max(1, Math.ceil((hi - lo) / 6))
  ) {
    chart.append(
      svg("line", { x1: 48, y1: y(t), x2: 768, y2: y(t), class: "chart-grid" }),
      svg(
        "text",
        { x: 38, y: y(t) + 4, "text-anchor": "end", class: "chart-label" },
        t + "°",
      ),
    );
  }
  if (caseId === "compressor")
    for (const t of [2, 8])
      chart.append(
        svg("line", {
          x1: 48,
          y1: y(t),
          x2: 768,
          y2: y(t),
          class: "chart-limit",
        }),
      );
  chart.append(
    svg("polyline", {
      points: ps.map((p, j) => `${x(j)},${y(p.cargo_temp_c)}`).join(" "),
      class: "chart-line",
    }),
  );
  chart.append(
    svg("line", { x1: x(i), y1: 20, x2: x(i), y2: 215, class: "chart-cursor" }),
    svg("circle", { cx: x(i), cy: y(p.cargo_temp_c), r: 5, fill: "#166b54" }),
  );
  chart.append(
    svg(
      "text",
      { x: 48, y: 243, class: "chart-label" },
      "Minute " + ps[0].minute,
    ),
    svg(
      "text",
      { x: 768, y: 243, "text-anchor": "end", class: "chart-label" },
      "Minute " + ps.at(-1).minute,
    ),
  );
}
function renderSteps() {
  const target = $("record-steps");
  target.replaceChildren();
  if (caseId !== "compressor") {
    target.append(
      el(
        "p",
        "This study has recorded telemetry, not a recorded intervention or approval. No decision history is invented for it.",
        "muted small",
      ),
    );
    return;
  }
  const q = $("record-search").value.trim().toLowerCase();
  const steps = data.trace.steps.filter(
    (s) =>
      !q ||
      [s.title, ...s.lines.map((l) => l.text)]
        .join(" ")
        .toLowerCase()
        .includes(q),
  );
  if (!steps.length)
    target.append(
      el("p", "No recorded step matches that search.", "muted small"),
    );
  for (const step of steps) {
    const details = el("details");
    details.id = "record-step-" + step.number;
    const summary = el("summary");
    summary.append(
      el("span", String(step.number).padStart(2, "0")),
      document.createTextNode(step.title || "Recording notes"),
    );
    const lines = el("ul", undefined, "record-lines");
    for (const [index, line] of step.lines.entries()) {
      if (!line.text) continue;
      const li = el(
        "li",
        line.text,
        ["good", "refused"].includes(line.kind) ? line.kind : "",
      );
      li.append(
        el(
          "small",
          `Source: original trace · step ${step.number} · line ${index + 1}`,
          "source-ref",
        ),
      );
      lines.append(li);
    }
    details.append(summary, lines);
    target.append(details);
  }
}
function ask(question) {
  const result = answerRecorded(question, data, caseId),
    answer = $("answer");
  answer.replaceChildren(
    el("div", result.explanation || result.text, "answer-summary"),
  );
  if (result.explanation) {
    const original = el("details", undefined, "answer-original");
    original.append(
      el("summary", "Read the original quoted answer"),
      el("div", result.text),
    );
    answer.append(original);
  }
  if (result.citations.length) {
    const refs = el("div", undefined, "answer-citations");
    refs.append(document.createTextNode("Recorded sources: "));
    for (const ref of result.citations) {
      const n = ref.match(/^trace.steps\[(\d+)\]$/);
      if (n) {
        const a = el("a", "Step " + n[1] + " ↗");
        a.href = "#record-step-" + n[1];
        a.addEventListener("click", (event) => {
          event.preventDefault();
          openRecord(Number(n[1]));
        });
        refs.append(a, document.createTextNode("  "));
      } else refs.append(el("span", ref));
    }
    answer.append(refs);
  }
}
function renderPlanning() {
  const p = planning();
  $("planning-risk-output").textContent = Math.round(p.probability * 100) + "%";
  const target = $("planning-options");
  target.replaceChildren();
  try {
    const ranked = rankPlanning(
      data.planning_policy,
      p.probability,
      p.value,
      p.blocked,
    );
    planningValid = true;
    $("planning-error").textContent = "";
    for (const option of ranked) {
      const li = el("li", undefined, option.feasible ? "" : "unavailable");
      li.append(
        el(
          "span",
          labels(option.id) + (option.feasible ? "" : " · unavailable"),
        ),
        el("b", option.feasible ? money(-option.ev) : "Excluded"),
      );
      li.title = option.basis;
      target.append(li);
    }
    $("practice-review").disabled = false;
    $("export").disabled = false;
    if ($("export-brief")) $("export-brief").disabled = false;
    $("review-status").textContent =
      reviewKey === planningKey(p, manifest.payload_sha256, caseId)
        ? "Reviewed in this browser rehearsal only. No backend approval was granted."
        : reviewKey
          ? "Assumptions changed. Review this rehearsal again; the earlier review no longer applies."
          : "Not reviewed. This never grants a backend approval.";
  } catch (error) {
    planningValid = false;
    $("planning-error").textContent = error.message;
    $("practice-review").disabled = true;
    $("export").disabled = true;
    if ($("export-brief")) $("export-brief").disabled = true;
    $("review-status").textContent =
      "Invalid assumptions. No planning decision or export is available.";
  }
}
function renderFault() {
  const id = $("fault-case").value,
    before = data.verification.before.rows.find((r) => r.id === id),
    after = data.verification.after.rows.find((r) => r.id === id);
  const target = $("fault-comparison");
  target.replaceChildren();
  for (const [label, row] of [
    ["Before the repair", before],
    ["After the repair", after],
  ]) {
    const box = el("div", undefined, "fault-box");
    box.append(
      el("small", label),
      el("strong", row.verdict),
      el("p", row.detail),
      el("small", "Constructed-case truth: " + row.truth),
    );
    target.append(box);
  }
  const b = data.verification.before,
    a = data.verification.after;
  $("fault-summary").textContent =
    `Entire frozen pack (${a.cases} cases): false confirmations ${b.false_confirmations} → ${a.false_confirmations}; missed recoveries ${b.missed_recoveries} → ${a.missed_recoveries}; abstentions ${b.abstentions} → ${a.abstentions}. This is a tradeoff, not field accuracy.`;
}
const notesKey = () =>
  `axon-workbench-notes-v1:${manifest.payload_sha256}:${caseId}`;
function loadNotes() {
  try {
    $("notes").value = (localStorage.getItem(notesKey()) || "").slice(0, 10000);
    $("notes-status").textContent =
      "Notes stay on this browser and this evidence version.";
  } catch {
    $("notes").value = "";
    $("notes-status").textContent =
      "Browser storage is unavailable. Export your notes to keep them.";
  }
}
function openRecord(number) {
  $("technical-details").open = true;
  $("record-search").value = "";
  renderSteps();
  const detail = $("record-step-" + number);
  if (detail) {
    detail.open = true;
    detail.scrollIntoView({ block: "start" });
  }
}
function renderStory(index, { moveCursor = true } = {}) {
  chapter = Math.max(0, Math.min(3, index));
  const chapters = getStory(data, caseId),
    current = chapters[chapter];
  document.querySelectorAll("[data-chapter]").forEach((button, i) => {
    button.setAttribute("aria-pressed", String(i === chapter));
    button.replaceChildren(
      el("span", String(i + 1)),
      document.createTextNode(chapters[i].label),
    );
  });
  $("story-counter").textContent = String(chapter + 1) + " of 4";
  $("story-kicker").textContent = current.kicker;
  $("story-title").textContent = current.title;
  $("story-copy").textContent = current.copy;
  $("story-callout").textContent = current.callout;
  $("story-boundary").textContent = current.boundary;
  $("story-previous").disabled = chapter === 0;
  $("story-next").textContent = chapter === 3 ? "Start again ↺" : "Next step →";
  $("story-evidence").textContent =
    caseId === "sensor" ? "See the test cases ↗" : "Show me why ↗";
  if (moveCursor) {
    stop();
    $("timeline").value = String(Math.min(current.minute, points().length - 1));
    renderSignal();
  }
  history.replaceState(null, "", studyHash(caseId, chapter));
}

function changeCase(next, initialChapter = 0) {
  if (!["compressor", "sensor"].includes(next)) return;
  stop();
  caseId = next;
  for (const b of document.querySelectorAll("[data-case]"))
    b.setAttribute("aria-pressed", String(b.dataset.case === next));
  $("case-type").textContent =
    next === "compressor"
      ? "Medicine delivery · recorded story"
      : "Sensor reading · comparison study";
  $("case-title").textContent =
    next === "compressor"
      ? "The cooling is failing. The delivery needs a closer look."
      : "A sensor can report a number. Can we trust it?";
  $("case-description").textContent =
    next === "compressor"
      ? "Follow one fictional medicine shipment from early warning to a reviewed response—and an outcome that still needed investigation."
      : "Explore a saved sensor-drift recording. There is no recorded approval, intervention or recovery for this second study.";
  $("fact-value").textContent =
    next === "compressor"
      ? data.trace.facts.lead_time_minutes + " min"
      : "Uncertain";
  $("fact-label").textContent =
    next === "compressor"
      ? "Single recorded incident: warning before its threshold alarm. Not a fleet average."
      : "Finite readings can still be wrong. No field sensor-trust claim.";
  $("telemetry-scope").textContent =
    next === "compressor"
      ? "Dashed reference limits: 2–8 °C, from the Bill of Lading described in recorded step 4. Full saved trajectory is visible; no new prediction is made."
      : "Observed cargo channel only; simulator ground truth is not loaded. This telemetry-only study does not establish a contractual decision or intervention.";
  $("timeline").max = String(points().length - 1);
  $("timeline").value = String(
    Math.min(data.trace.facts.detected_at_minute, points().length - 1),
  );
  $("record-search").value = "";
  $("answer").replaceChildren();
  loadNotes();
  renderSteps();
  renderSignal();
  renderPlanning();
  $("move-status").textContent =
    next === "compressor"
      ? "Proposed in the original recording"
      : "More evidence needed";
  $("move-title").textContent =
    next === "compressor"
      ? "Move the cargo to cold storage."
      : "Check before calling it safe.";
  $("move-copy").textContent =
    next === "compressor"
      ? "The saved comparison favored a cold-storage reroute. A person still had to review the current evidence. The later result did not establish a recovery."
      : "A single sensor channel cannot establish physical truth. This study does not contain a recorded approved response.";
  $("move-source").textContent =
    next === "compressor"
      ? "See the original recommendation ↗"
      : "Explore the sensor test cases ↗";
  renderStory(initialChapter);
}
function packet() {
  if (!planningValid)
    throw Error("Correct the planning inputs before exporting.");
  const p = makePacket({
    data,
    manifest,
    caseId,
    notes: $("notes").value,
    planning: planning(),
    reviewKey,
  });
  const id = $("fault-case").value;
  p.selected_observation = points()[Number($("timeline").value)];
  p.trace_facts = caseId === "compressor" ? data.trace.facts : null;
  p.constructed_verifier_case = {
    id,
    scope: data.verification.scope,
    before: data.verification.before.rows.find((r) => r.id === id),
    after: data.verification.after.rows.find((r) => r.id === id),
  };
  return p;
}
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function fetchText(name) {
  const response = await fetch(new URL(name, import.meta.url), {
    cache: "no-store",
    signal: AbortSignal.timeout(20000),
  });
  if (!response.ok) throw Error("A recorded evidence file is unavailable.");
  const text = await response.text();
  if (text.length > 2_000_000)
    throw Error("Recorded evidence exceeded the workspace size limit.");
  return text;
}
async function start() {
  try {
    const [receipt, text, identity] = await Promise.all([
      fetchText("./workbench-manifest.json"),
      fetchText("./workbench-data.json"),
      fetchText("./workbench-release.json")
        .then(JSON.parse)
        .catch(() => null),
    ]);
    manifest = JSON.parse(receipt);
    data = await verifyBundle(manifest, text);
    $("load-status").hidden = true;
    $("app-shell").hidden = false;
    const model = data.trace.llm_showcase;
    $("model-identity").textContent = model?.available
      ? "Recorded provider: " +
        String(model.vendor) +
        " · Models: " +
        Object.entries(model.models || {})
          .map(([role, name]) => role + ": " + String(name))
          .join(" · ")
      : "No model showcase is available in this recording.";
    $("model-grounding").textContent =
      model?.narrative_grounded === true
        ? "The recorded deterministic grounding check passed. This does not establish model accuracy."
        : "Grounding is not established for this snapshot.";
    $("model-narrative").textContent = model?.available
      ? String(model.narrative || "No narrative recorded.")
      : "No substitute narrative has been generated.";
    $("recording-date").textContent =
      "Original recording: " +
      new Date(data.recorded_at).toISOString() +
      " · fictional carrier data · fingerprint checked";
    const proof = $("proof-details");
    for (const [label, value] of [
      ["Recording", data.recorded_at],
      ["Dataset fingerprint", manifest.payload_sha256],
      ["Source files", String(data.sources.length)],
      ["Model mode", "Saved record; no live inference"],
    ]) {
      const row = el("div");
      row.append(el("dt", label), el("dd", value));
      proof.append(row);
    }
    if (
      identity?.schema === 1 &&
      /^[a-f0-9]{40}$/.test(identity.source_commit || "")
    ) {
      const row = el("div");
      row.append(
        el(
          "dt",
          identity.dirty_source
            ? "Local build (uncommitted source)"
            : "Workspace build",
        ),
      );
      const value = el("dd"),
        link = el(
          "a",
          identity.source_commit.slice(0, 12) + " · inspect source ↗",
        );
      link.href =
        "https://github.com/dilipna/axon-fde/tree/" + identity.source_commit;
      link.target = "_blank";
      link.rel = "noreferrer";
      value.append(link);
      row.append(value);
      proof.append(row);
    } else {
      const row = el("div");
      row.append(
        el("dt", "Workspace build"),
        el(
          "dd",
          "Build identity unavailable; original evidence fingerprints remain available.",
        ),
      );
      proof.append(row);
    }
    for (const source of data.sources) {
      const li = el("li");
      li.append(el("b", source.path), el("div", source.sha256));
      $("source-list").append(li);
    }
    for (const row of data.verification.after.rows) {
      const option = el("option", row.id.replaceAll("_", " "));
      option.value = row.id;
      $("fault-case").append(option);
    }
    document
      .querySelectorAll("[data-case]")
      .forEach((b) =>
        b.addEventListener("click", () => changeCase(b.dataset.case)),
      );
    $("record-search").addEventListener("input", renderSteps);
    $("timeline").addEventListener("input", () => {
      stop();
      renderSignal();
    });
    $("play").disabled = reduced.matches;
    reduced.addEventListener("change", () => {
      stop();
      $("play").disabled = reduced.matches;
    });
    $("play").addEventListener("click", () => {
      if (timer) {
        stop();
        return;
      }
      if (Number($("timeline").value) >= points().length - 1)
        $("timeline").value = "0";
      $("play").textContent = "Pause recorded timeline";
      timer = setInterval(() => {
        const next = Number($("timeline").value) + 1;
        $("timeline").value = String(Math.min(next, points().length - 1));
        renderSignal();
        if (next >= points().length - 1) stop();
      }, 250);
    });
    $("fault-case").addEventListener("change", renderFault);
    for (const id of [
      "planning-risk",
      "planning-value",
      "facility-unavailable",
      "trailer-unavailable",
    ])
      $(id).addEventListener("input", renderPlanning);
    $("practice-review").addEventListener("click", () => {
      if (planningValid) {
        reviewKey = planningKey(planning(), manifest.payload_sha256, caseId);
        renderPlanning();
      }
    });
    $("notes").addEventListener("input", () => {
      try {
        localStorage.setItem(notesKey(), $("notes").value.slice(0, 10000));
        $("notes-status").textContent = "Saved on this browser.";
      } catch {
        $("notes-status").textContent =
          "Could not save in browser storage. Export your notes instead.";
      }
    });
    $("clear-notes").addEventListener("click", () => {
      try {
        localStorage.removeItem(notesKey());
      } catch {}
      $("notes").value = "";
      $("notes-status").textContent = "This study’s notes are cleared.";
    });
    $("question-form").addEventListener("submit", (event) => {
      event.preventDefault();
      ask($("question").value);
    });
    document.querySelectorAll("[data-question]").forEach((b) =>
      b.addEventListener("click", () => {
        $("question").value = b.dataset.question;
        ask(b.dataset.question);
      }),
    );
    $("export").addEventListener("click", () => {
      try {
        download(
          "coldpath-" + caseId + "-decision-packet.json",
          JSON.stringify(packet(), null, 2),
          "application/json",
        );
        tell(
          "Decision packet downloaded with source fingerprints, notes and rehearsal scope.",
        );
      } catch (error) {
        tell(error.message);
      }
    });
    const brief = el(
      "button",
      "Download my readable brief ↓",
      "quiet full-width",
    );
    brief.id = "export-brief";
    brief.addEventListener("click", () => {
      try {
        const p = packet();
        const text = makeBrief(p);
        download(
          "coldpath-" + caseId + "-shift-brief.md",
          text,
          "text/markdown",
        );
        tell("Shift brief downloaded.");
      } catch (error) {
        tell(error.message);
      }
    });
    $("export").before(brief);
    $("share").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(location.href);
        tell("Study link copied. Notes remain private to your browser.");
      } catch {
        tell("Copy this study link: " + location.href);
      }
    });

    document.addEventListener("keydown", (event) => {
      if (
        event.key === "/" &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(
          document.activeElement.tagName,
        )
      ) {
        event.preventDefault();
        $("technical-details").open = true;
        $("record-search").focus();
      }
    });
    document
      .querySelectorAll("[data-chapter]")
      .forEach((button) =>
        button.addEventListener("click", () =>
          renderStory(Number(button.dataset.chapter)),
        ),
      );
    $("return-story").addEventListener("click", () => {
      $("story-title").focus({ preventScroll: true });
      $("story-title").scrollIntoView({ block: "start" });
    });
    $("story-next").addEventListener("click", () =>
      renderStory((chapter + 1) % 4),
    );
    $("story-previous").addEventListener("click", () =>
      renderStory(chapter - 1),
    );
    $("story-evidence").addEventListener("click", () => {
      const story = getStory(data, caseId)[chapter];
      if (story.sources.length) openRecord(story.sources[0]);
      else {
        $("technical-details").open = true;
        $("fault-case").focus();
      }
    });
    $("move-source").addEventListener("click", (event) => {
      event.preventDefault();
      if (caseId === "compressor") openRecord(8);
      else {
        $("technical-details").open = true;
        $("fault-case").focus();
      }
    });
    const requested = readStudyLocation(location.hash);
    changeCase(requested.caseId, requested.chapter);
    renderFault();
    renderPlanning();
  } catch (error) {
    $("app-shell").hidden = true;
    $("load-status").hidden = false;
    $("load-status").classList.add("error");
    $("load-status").setAttribute("role", "alert");
    $("load-status").replaceChildren(
      el("strong", "Recorded evidence could not be verified. "),
      document.createTextNode(
        error.message + " No decision or export is available.",
      ),
      el(
        "p",
        "Reload after checking the source files. No substitute data has been invented.",
      ),
    );
  }
}
$("about-open").addEventListener("click", () => $("about-dialog").showModal());
start();
