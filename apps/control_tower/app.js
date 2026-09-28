/* AxonFDE site.
 *
 * Reads three endpoints and renders them. There is deliberately no fallback to
 * sample data anywhere in this file: if a fetch fails the section says what is
 * missing and which command produces it. A page that looks the same whether
 * or not the system ran is worse than one that is plainly empty.
 *
 * Every animation is driven by the recorded run. The truck's display, the
 * replay gauges and the event feed all read the served telemetry and the
 * trace's recorded facts; nothing is scripted to "look right". The one
 * illustrative element is the truck's position on the route map, and the
 * page says so beside it - the recording has no GPS.
 */

const API = "/api/v1";

const PERMITTED_MIN_C = 2.0;
const PERMITTED_MAX_C = 8.0;

const REDUCED_MOTION =
  typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

/* Which claims get a card, in the order they are shown, and how to phrase
 * each one for a reader outside the domain. Held here rather than derived from
 * the run so that a claim the harness starts reporting does not silently
 * appear unlabelled. */
const CLAIM_CARDS = [
  {
    id: "C1",
    plain: "Warns earlier than a standard alarm",
    label: "median lead time over the threshold alarm",
    format: (r) => `${fmt(r.value)} min`,
    /* C1 may never be shown alone. claims.md calls the unpaired number a
     * misuse, so the card refuses to render without its companion. */
    pair: c1Pair,
    required: true,
  },
  {
    id: "C6",
    plain: "Catches paperwork that disagrees",
    label: "of deliberately planted conflicts between the order system and the shipping papers were found",
    format: (r) => pct(r.value),
    pair: (r) =>
      r.companions?.precision === undefined
        ? null
        : `precision ${pct(r.companions.precision)} over ${fmt(r.companions.negative_cases)} look-alike shipments with no conflict`,
  },
  {
    id: "C7",
    plain: "Never acts without approval",
    label: "unauthorised actions across every role × action combination and gate attempt",
    format: (r) => fmt(r.value),
    zeroIsGood: true,
  },
  {
    id: "C8",
    plain: "Never runs a forbidden database command",
    label: "prohibited commands that got through, out of a battery of hostile queries",
    format: (r) => fmt(r.value),
    zeroIsGood: true,
  },
];

/* Plain-language headline for each recorded step of the closed loop, keyed by
 * the step's recorded title so a renumbered or renamed step shows its own
 * title rather than someone else's explanation. */
const PLAIN_STEPS = {
  "Read the shipment from the legacy ERP":
    "Looks up the order in the company's existing system — read-only, so nothing there can be changed.",
  "Replay 240 minutes of telemetry through the predictive detector":
    "Streams every sensor reading from the trip through the early-warning model.",
  "Reconcile the sources that disagree":
    "Notices that the order system and the signed shipping papers give different temperature limits.",
  "Resolve the envelope this cargo is judged against":
    "Uses the stricter, signed limit — judged by the other one, this load would never look at risk.",
  "Detect":
    "Raises the alarm early, while the cargo is still within its safe range.",
  "Estimate risk, and store it beside its baseline":
    "Estimates the chance of spoiling within the hour, next to a simple rule-based estimate for comparison.",
  "Check which interventions are actually possible":
    "Checks which nearby cold-storage sites actually have space.",
  "Rank every option by expected value, including doing nothing":
    "Prices every option in dollars, including doing nothing, and ranks them.",
  "Request approval, bound to exactly what the approver is shown":
    "Asks a fleet manager to approve, locked to exactly the evidence they were shown.",
  "A fleet manager grants it":
    "The manager approves.",
  "The world moves before the action is taken":
    "New readings arrive before the action runs, so the old approval is refused as out of date.",
  "Re-seek approval against the world as it now is, then act":
    "A fresh approval is given; the truck is rerouted — once, even when the command is retried.",
  "Verify the outcome, then verify the record of all of it":
    "Checks whether it worked. In this recording the truck was never actually rerouted, so it didn't — and the case is honestly reopened.",
};

const fmt = (n, dp = 0) =>
  n === undefined || n === null ? "—" : Number(n).toFixed(dp);
const pct = (n) => `${(Number(n) * 100).toFixed(0)}%`;
const el = (sel) => document.querySelector(sel);
const humanise = (s) => {
  const t = String(s).replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
};
/* The terminal narrator colours some words, and the trace records the escape
 * codes with them. They are presentation for a terminal, not content. */
// eslint-disable-next-line no-control-regex
const stripAnsi = (s) => String(s).replace(/\x1b\[[0-9;]*m/g, "");

function c1Pair(r) {
  const far = r.companions?.false_alarm_rate;
  const base = r.companions?.baseline_false_alarm_rate;
  if (far === undefined) return null;
  return `at a ${pct(far)} false-alarm rate${
    base === undefined ? "" : ` — the standard alarm's is ${pct(base)}`
  }`;
}

async function getJSON(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let detail = { reason: `${res.status} ${res.statusText}`, fix: "" };
    try {
      const body = await res.json();
      if (body?.detail && typeof body.detail === "object") detail = body.detail;
    } catch {
      /* a non-JSON error body is still an error; the status line carries it */
    }
    throw Object.assign(new Error(detail.reason), detail);
  }
  return res.json();
}

function showEmpty(node, err) {
  if (!node) return;
  node.innerHTML = "";
  const box = document.createElement("div");
  box.className = "empty";
  box.textContent = err.reason || err.message || "Not available.";
  if (err.fix) {
    const code = document.createElement("code");
    code.textContent = err.fix;
    box.appendChild(document.createElement("br"));
    box.appendChild(code);
  }
  node.appendChild(box);
}

const SVG = "http://www.w3.org/2000/svg";
const mk = (name, attrs = {}) => {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
};

/* ── Claims ───────────────────────────────────────────────────────── */

function renderClaims(run) {
  const grid = el("#claim-grid");
  grid.innerHTML = "";

  const byId = Object.fromEntries((run.results || []).map((r) => [r.claim_id, r]));

  for (const card of CLAIM_CARDS) {
    const r = byId[card.id];
    if (!r || r.status !== "MEASURED") continue;
    const pair = card.pair?.(r);
    if (card.required && !pair) continue;

    const node = document.createElement("div");
    node.className = "claim";

    const plain = document.createElement("div");
    plain.className = "claim-plain";
    plain.textContent = card.plain;

    const value = document.createElement("div");
    value.className = "claim-value";
    if (card.zeroIsGood && Number(r.value) === 0) value.classList.add("zero");
    value.textContent = card.format(r);

    const label = document.createElement("div");
    label.className = "claim-label";
    label.textContent = card.label;

    node.append(plain, value, label);

    if (pair) {
      const p = document.createElement("div");
      p.className = "claim-pair";
      p.textContent = pair;
      node.appendChild(p);
    }

    const cases = document.createElement("div");
    cases.className = "claim-cases";
    cases.textContent = `${card.id} · measured over ${r.cases} ${r.case_unit} (${r.required_cases} required)`;
    node.appendChild(cases);

    grid.appendChild(node);
  }

  /* The second median. Shown next to the headline rather than buried, because
   * the headline overstates the warning that is actually attributable to the
   * fault. */
  const c1 = byId.C1;
  const after = c1?.companions?.median_lead_time_after_fault_onset_min;
  const early = c1?.companions?.alerts_preceding_fault_onset_rate;
  if (after !== undefined && early !== undefined) {
    const note = el("#c1-caveat");
    note.hidden = false;
    note.innerHTML =
      `<strong>The honest fine print.</strong> On ${pct(early)} of the simulated breaches the ` +
      `early warning fired <strong>before the fault had even started</strong> — on hot days the ` +
      `cargo genuinely warms for a while as the cooling unit settles, and a trend-based warning ` +
      `cannot tell that apart from a real problem. Counting only warnings that came after the ` +
      `fault, the median is <strong>${fmt(after)} minutes</strong>. Both numbers are published; ` +
      `we did not tune the model against the test that measures it.`;
  }

  /* Hero stats. C1 goes on the band only with its false-alarm rate. */
  const c1pair = c1 && c1.status === "MEASURED" ? c1Pair(c1) : null;
  if (c1pair) {
    el("#st-median").innerHTML = `${fmt(c1.value)}<small>min</small>`;
    el("#st-median-k").innerHTML =
      `median early warning across ${c1.cases} simulated breaches` +
      `<span class="pair">${c1pair}</span>`;
  }
  const c7 = byId.C7;
  if (c7 && c7.status === "MEASURED") el("#st-unauth").textContent = fmt(c7.value);

  const p = run.provenance || {};
  el("#run-id").textContent = p.run_id || "—";
  const bar = el("#provenance");
  bar.innerHTML = "";
  const pills = [
    `run ${p.run_id ?? "—"}`,
    `code ${p.git_sha ?? "—"}`,
    `scenario pack ${p.pack_version ?? "—"}`,
    `model ${p.model_id ?? "none"}`,
  ];
  for (const text of pills) {
    const pill = document.createElement("span");
    pill.className = "pill";
    pill.textContent = text;
    bar.appendChild(pill);
  }
  if (p.reproducible === false) {
    const warn = document.createElement("span");
    warn.className = "pill pill-warn";
    warn.textContent = "not reproducible — do not quote";
    bar.appendChild(warn);
  }

  renderVerdict(byId);
}

/* ── The AI ───────────────────────────────────────────────────────── */

function renderShowcase(show) {
  const hyps = el("#hyps");
  if (!show || !show.available) {
    showEmpty(hyps, {
      reason: "No model investigation was recorded with this run.",
      fix: "uv run poe demo-trace",
    });
    el("#narrative").textContent = "—";
    return;
  }

  hyps.innerHTML = "";
  for (const h of show.hypotheses || []) {
    const row = document.createElement("div");
    row.className = "hyp";
    row.innerHTML =
      `<div class="hyp-top"><span>${humanise(h.root_cause)}</span><b>${pct(h.confidence)}</b></div>` +
      `<div class="hyp-bar"><i data-w="${(h.confidence * 100).toFixed(1)}"></i>` +
      `<b style="left:calc(${(h.prior * 100).toFixed(1)}% - 1px)"></b></div>` +
      `<div class="hyp-meta">rules allow up to ${pct(h.prior)} · evidence cited: ${
        (h.cited || []).join(", ") || "none"
      }</div>`;
    hyps.appendChild(row);
  }
  /* Grow the bars once they are on screen. */
  requestAnimationFrame(() =>
    hyps.querySelectorAll(".hyp-bar i").forEach((i) => (i.style.width = `${i.dataset.w}%`))
  );

  el("#narrative").textContent = (show.narrative || "").trim() || "—";
  const g = el("#grounded");
  g.className = `grounded ${show.narrative_grounded ? "ok" : "bad"}`;
  g.textContent = show.narrative_grounded
    ? `Fact-checked: ${stripGrounded(show.grounding_detail)}`
    : `Failed the fact check: ${stripGrounded(show.grounding_detail)}`;

  const models = show.models || {};
  const cost = (show.invocations || []).reduce((s, i) => s + (i.cost_usd || 0), 0);
  const replayed = (show.invocations || []).every((i) => i.replayed);
  el("#ai-model").textContent =
    `${models.propose_links ?? "?"} ranked the causes and ${models.narrate ?? "?"} wrote the text, ` +
    `via ${show.vendor ?? "?"}` +
    (cost ? `, for $${cost.toFixed(5)}` : "") +
    (replayed ? ". Replayed from a recorded session — this page makes no live model calls." : ".") +
    ` The fact check is ordinary code, not another AI: every cited item and every number must match the evidence.`;
}

const stripGrounded = (s) => String(s || "").replace(/^grounded:\s*/, "");

function renderVerdict(byId) {
  const c5 = byId.C5;
  if (!c5 || c5.status !== "MEASURED") return;
  const c = c5.companions || {};
  const c12 = byId.C12?.companions || {};
  const c10 = byId.C10;

  const llmTop1 = c.rules_llm_top1_accuracy;
  const rulesTop1 = c.rules_only_top1_accuracy ?? (llmTop1 - c5.value);
  const cells = [
    {
      k: "Picks the right root cause",
      v: `${pct(rulesTop1)}<span class="arrow">→</span>${pct(llmTop1)}`,
      d: "rules alone → rules + AI. Worse.",
    },
    {
      k: "Chooses the right action",
      v: `${pct(c.rules_only_correct_action_rate)}<span class="arrow">→</span>${pct(c.rules_llm_correct_action_rate)}`,
      d: `identical on ${fmt(c.incidents_with_identical_action)} of ${fmt(c5.cases)} — by design, the AI never chooses`,
    },
    {
      k: "Explanation quality (of 5)",
      v: `${fmt(c.rules_only_explanation_score, 1)}<span class="arrow">→</span>${fmt(c.rules_llm_explanation_score, 1)}`,
      d: "the one place it clearly helped",
    },
    {
      k: "Cost per incident",
      v: c12.cost_usd_p95 === undefined ? "—" : `$${Number(c12.cost_usd_p95).toFixed(4)}`,
      d: c12.latency_s_p50 === undefined ? "95th percentile" : `95th percentile · ${fmt(c12.latency_s_p50, 1)} s typical`,
    },
  ];
  el("#ai-vs").innerHTML = cells
    .map((x) => `<div class="vs-cell"><div class="vs-k">${x.k}</div><div class="vs-v">${x.v}</div><div class="vs-d">${x.d}</div></div>`)
    .join("");

  el("#ai-verdict-note").textContent =
    `Measured over ${c5.cases} ${c5.case_unit} with a small, free model (gpt-oss on Groq), not a frontier model — ` +
    `so this is a verdict on this model, not on AI in general.` +
    (c10 && c10.status === "MEASURED"
      ? ` ${pct(c10.value)} of its explanations contained a figure the fact check could not match exactly, and were flagged rather than shown as correct.`
      : "") +
    ` A negative result we measured ourselves is worth more than a cherry-picked win.`;
  el("#ai-verdict").hidden = false;
}

/* ── Hero: the truck's display replays the recording ──────────────── */

function startHero(points, facts) {
  const temp = el("#hero-temp");
  const minEl = el("#hero-min");
  const disp = el("#hero-display");
  const led = el("#hero-led");
  const cap = el("#hero-cap");
  const detect = facts.detected_at_minute;
  const alarm = facts.baseline_alarm_minute;

  const show = (i) => {
    const p = points[i];
    temp.textContent = `${p.cargo_temp_c.toFixed(1)}°C`;
    minEl.textContent = `MIN ${p.minute}`;
    const state = alarm != null && p.minute >= alarm ? "alarm" : detect != null && p.minute >= detect ? "warn" : "";
    disp.setAttribute("class", state);
    led.setAttribute("fill", state === "alarm" ? "var(--rose)" : state === "warn" ? "var(--cyan)" : "var(--good)");
    cap.textContent =
      state === "alarm"
        ? `Minute ${p.minute}: the standard alarm finally fires — the cargo is already too warm.`
        : state === "warn"
        ? `Minute ${p.minute}: AxonFDE has warned. A standard alarm is still silent.`
        : `Minute ${p.minute}: every reading is inside the safe range. Nothing looks wrong yet.`;
  };

  if (REDUCED_MOTION) {
    show(Math.min(points.length - 1, detect ?? 0));
    return;
  }
  let i = 0;
  const tick = () => {
    show(i);
    /* Linger at the two moments that matter, then loop. */
    const p = points[i];
    const hold = p.minute === detect || p.minute === alarm ? 1800 : 70;
    i = (i + 1) % points.length;
    setTimeout(tick, i === 0 ? 2500 : hold);
  };
  tick();
}

/* ── Chart ────────────────────────────────────────────────────────── */

let chartGeom = null;

function renderChart(points, marks) {
  const svg = el("#chart");
  svg.innerHTML = "";
  if (!points.length) return;

  const W = svg.clientWidth || 1100;
  const H = 340;
  const pad = { t: 30, r: 18, b: 34, l: 44 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const temps = points.map((p) => p.cargo_temp_c);
  const lo = Math.min(PERMITTED_MIN_C, ...temps) - 1;
  const hi = Math.max(PERMITTED_MAX_C, ...temps) + 1;
  const maxMin = points[points.length - 1].minute;

  const x = (m) => pad.l + (m / maxMin) * (W - pad.l - pad.r);
  const y = (t) => pad.t + (1 - (t - lo) / (hi - lo)) * (H - pad.t - pad.b);

  const defs = mk("defs");
  defs.innerHTML = `<clipPath id="played"><rect id="played-rect" x="0" y="0" width="${W}" height="${H}"/></clipPath>`;
  svg.appendChild(defs);

  const step = hi - lo > 14 ? 4 : 2;
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) {
    svg.appendChild(mk("line", { x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t), stroke: "#1a1a1a" }));
  }

  /* The safe range as a band, so "in spec" is a region rather than a number
   * the viewer has to hold in their head. */
  svg.appendChild(
    mk("rect", {
      x: pad.l,
      y: y(PERMITTED_MAX_C),
      width: W - pad.l - pad.r,
      height: Math.max(0, y(PERMITTED_MIN_C) - y(PERMITTED_MAX_C)),
      fill: "rgba(255,255,255,.05)",
      stroke: "rgba(255,255,255,.25)",
      "stroke-dasharray": "2 4",
    })
  );
  for (const t of [PERMITTED_MIN_C, PERMITTED_MAX_C]) {
    const label = mk("text", { x: 6, y: y(t) + 4, fill: "#8c8c8c", "font-size": "12", "font-family": "Arial" });
    label.textContent = `${t}°C`;
    svg.appendChild(label);
  }

  const d = points
    .map((p, i) => `${i ? "L" : "M"}${x(p.minute).toFixed(1)},${y(p.cargo_temp_c).toFixed(1)}`)
    .join(" ");
  /* The whole trip faintly, and the part the replay has reached in white. */
  svg.appendChild(mk("path", { d, fill: "none", stroke: "#3a3a3a", "stroke-width": 2, "stroke-linejoin": "round" }));
  svg.appendChild(
    mk("path", { d, fill: "none", stroke: "#ffffff", "stroke-width": 2.2, "stroke-linejoin": "round", "clip-path": "url(#played)" })
  );

  marks.forEach((m, idx) => {
    if (m.minute === null || m.minute === undefined) return;
    svg.appendChild(
      mk("line", {
        x1: x(m.minute), x2: x(m.minute), y1: pad.t, y2: H - pad.b,
        stroke: m.colour, "stroke-width": 2, "stroke-dasharray": m.dash ?? "0",
      })
    );
    /* Direct label, in ink rather than the series colour, on a plate. */
    const text = `${m.label} · minute ${m.minute}`;
    const w = text.length * 6.6 + 16;
    /* The two marks are 35 minutes apart, closer than two labels are wide,
     * so the earlier one is labelled to the left of its line and the later
     * one to the right - unless that would run off the chart. */
    const left = (idx === 0 && x(m.minute) - w - 6 > pad.l) || x(m.minute) + w + 6 > W - pad.r;
    const lx = left ? x(m.minute) - w - 6 : x(m.minute) + 6;
    svg.appendChild(mk("rect", { x: lx, y: pad.t - 24, width: w, height: 20, rx: 10, fill: "#000", stroke: m.colour }));
    const label = mk("text", { x: lx + 8, y: pad.t - 10, fill: "#fff", "font-size": "12", "font-weight": "700", "font-family": "Arial" });
    label.textContent = text;
    svg.appendChild(label);
  });

  const axis = mk("text", { x: W - pad.r, y: H - 8, fill: "#8c8c8c", "font-size": "12", "text-anchor": "end", "font-family": "Arial" });
  axis.textContent = "minutes since departure";
  svg.appendChild(axis);

  const head = mk("line", { y1: pad.t, y2: H - pad.b, stroke: "#fff", "stroke-opacity": 0.35, "stroke-width": 1 });
  const dot = mk("circle", { r: 5, fill: "#fff", stroke: "#000", "stroke-width": 2 });
  svg.append(head, dot);

  el("#legend").innerHTML = [
    `<span><i class="swatch" style="background:#fff"></i>reported cargo temperature</span>`,
    `<span><i class="swatch box" style="background:rgba(255,255,255,.08);border:1px dashed rgba(255,255,255,.4)"></i>safe range (${PERMITTED_MIN_C}–${PERMITTED_MAX_C} °C, from the signed shipping papers)</span>`,
    ...marks
      .filter((m) => m.minute !== null && m.minute !== undefined)
      .map(
        (m) =>
          `<span><i class="swatch${m.dash ? " dash" : ""}" style="background:${m.colour};border-color:${m.colour}"></i>${m.legend}</span>`
      ),
  ].join("");

  chartGeom = { x, y, W, H, pad, head, dot, maxMin };
  attachCrosshair(svg, points, marks, chartGeom);
}

function setChartPlayhead(p) {
  if (!chartGeom || !p) return;
  const px = chartGeom.x(p.minute);
  const rect = document.getElementById("played-rect");
  if (rect) rect.setAttribute("width", String(px));
  chartGeom.head.setAttribute("x1", px);
  chartGeom.head.setAttribute("x2", px);
  chartGeom.dot.setAttribute("cx", px);
  chartGeom.dot.setAttribute("cy", chartGeom.y(p.cargo_temp_c));
}

/* Crosshair + tooltip: hover reads out the exact reported value and what the
 * system had concluded by then. Values come from the served points; nothing
 * is interpolated for display. */
function attachCrosshair(svg, points, marks, g) {
  const wrap = svg.parentElement;
  wrap.querySelector(".tip")?.remove();
  const tip = document.createElement("div");
  tip.className = "tip";
  wrap.appendChild(tip);

  const detect = marks.find((m) => m.key === "axon")?.minute;
  const alarm = marks.find((m) => m.key === "baseline")?.minute;

  svg.onmousemove = (ev) => {
    const box = svg.getBoundingClientRect();
    const vx = ((ev.clientX - box.left) / box.width) * g.W;
    const minute = Math.max(0, Math.min(g.maxMin, ((vx - g.pad.l) / (g.W - g.pad.l - g.pad.r)) * g.maxMin));
    const p = points.reduce((best, q) => (Math.abs(q.minute - minute) < Math.abs(best.minute - minute) ? q : best));
    const state =
      alarm != null && p.minute >= alarm
        ? "the standard alarm has fired"
        : detect != null && p.minute >= detect
        ? "AxonFDE has already warned"
        : "no alert yet";
    tip.innerHTML =
      `<div class="t">minute ${p.minute}</div><div>reported <b>${p.cargo_temp_c.toFixed(2)} °C</b></div>` +
      `<div class="state">${state}</div>`;
    const px = g.x(p.minute);
    const tx = (px / g.W) * box.width;
    tip.style.left = `${Math.min(tx + 14, box.width - 180)}px`;
    tip.style.top = `${Math.max(0, (g.y(p.cargo_temp_c) / g.H) * box.height - 80)}px`;
    tip.classList.add("on");
  };
  svg.onmouseleave = () => tip.classList.remove("on");
}

/* The two minutes the chart marks, read from the facts the run recorded.
 *
 * An earlier version recovered them with a regex over the narration. It
 * matched the detection minute, missed the baseline alarm, and drew a chart
 * with one line instead of two — losing precisely the comparison the lead-time
 * claim is about, while looking like a chart that had rendered fine. Prose is
 * for people; these are for the chart. */
function detectionMarks(trace) {
  const f = trace.facts || {};
  const marks = [];
  if (f.detected_at_minute != null) {
    marks.push({
      key: "axon",
      minute: Number(f.detected_at_minute),
      colour: "var(--cyan)",
      label: "AxonFDE warns",
      legend: "AxonFDE's early warning",
    });
  }
  if (f.baseline_alarm_minute != null) {
    marks.push({
      key: "baseline",
      minute: Number(f.baseline_alarm_minute),
      colour: "var(--rose)",
      dash: "6 4",
      label: "Standard alarm",
      legend: "standard alarm — the cargo is already out of range",
    });
  }
  return marks;
}

/* ── Replay: map, gauges, feed ────────────────────────────────────── */

/* Stylised geography. The route and sites are drawn, not surveyed: the
 * recording has no GPS, and the page says so under the map. */
const ROUTE_D = "M70,120 C150,170 240,100 330,160 S520,250 600,236 S680,262 700,282";
const SITE_POS = {
  "CS-11": [150, 205],
  "CS-12": [372, 104],
  "CS-13": [520, 332],
};

function parseTrip(trace) {
  const first = (trace.steps || []).find((s) => s.number === 1);
  const line = first?.lines.find((l) => l.kind === "good")?.text ?? "";
  const m = stripAnsi(line).match(/—\s*(.+?),\s*(.+?)\s*->\s*(.+?),\s*[A-Z]{2}\b/);
  const cities = stripAnsi(line).match(/,\s*([^,]+),\s*[A-Z]{2}\s*->\s*([^,]+),\s*[A-Z]{2}/);
  return {
    customer: m ? m[1] : null,
    from: cities ? cities[1].trim() : "Origin",
    to: cities ? cities[2].trim() : "Destination",
  };
}

function parseSites(trace) {
  const step = (trace.steps || []).find((s) => /interventions/i.test(s.title || ""));
  if (!step) return [];
  return step.lines
    .map((l) => stripAnsi(l.text).match(/^(CS-\d+)\s+(.+?):\s*(.+)$/))
    .filter(Boolean)
    .map(([, id, name, detail]) => ({ id, name, detail, full: /full/i.test(detail) }));
}

function parseRecommendation(trace) {
  const step = (trace.steps || []).find((s) => /expected value/i.test(s.title || ""));
  const top = step?.lines.find((l) => stripAnsi(l.text).trim().startsWith(">"));
  const m = top && stripAnsi(top.text).match(/>\s*([a-z_]+)/);
  return m ? humanise(m[1]).toLowerCase() : null;
}

function drawMap(trip, sites) {
  const svg = el("#map");
  svg.innerHTML = "";

  /* A dotted field, for texture. */
  const dots = mk("g", { fill: "#1c1c1c" });
  for (let gx = 20; gx < 800; gx += 24) for (let gy = 20; gy < 380; gy += 24) dots.appendChild(mk("circle", { cx: gx, cy: gy, r: 1.3 }));
  svg.appendChild(dots);

  const bg = mk("path", { d: ROUTE_D, class: "route-bg" });
  const done = mk("path", { d: ROUTE_D, class: "route-done" });
  svg.append(bg, done);

  for (const s of sites) {
    const pos = SITE_POS[s.id];
    if (!pos) continue;
    const [sx, sy] = pos;
    svg.appendChild(mk("rect", { x: sx - 7, y: sy - 7, width: 14, height: 14, rx: 3, fill: s.full ? "#000" : "#fff", stroke: "#fff", "stroke-width": 2 }));
    const t = mk("text", { x: sx + 14, y: sy - 2, class: "map-city" });
    t.textContent = s.name;
    const t2 = mk("text", { x: sx + 14, y: sy + 14, class: "map-site" });
    t2.textContent = `${s.id} · ${s.detail}`;
    svg.append(t, t2);
  }

  const start = bg.getPointAtLength ? bg.getPointAtLength(0) : { x: 70, y: 120 };
  const len = bg.getTotalLength ? bg.getTotalLength() : 0;
  const end = len ? bg.getPointAtLength(len) : { x: 700, y: 282 };
  /* Labels sit above-left of the origin and above-right of the destination,
   * clear of the route line, which leaves one and arrives at the other. */
  for (const [p, name, dx, dy] of [[start, trip.from, -8, -16], [end, trip.to, 14, -14]]) {
    svg.appendChild(mk("circle", { cx: p.x, cy: p.y, r: 7, fill: "#000", stroke: "#fff", "stroke-width": 3 }));
    const t = mk("text", { x: p.x + dx, y: p.y + dy, class: "map-city" });
    t.textContent = name;
    svg.appendChild(t);
  }

  const legend = mk("text", { x: 20, y: 368, class: "map-site" });
  legend.textContent = "■ cold storage with space   □ cold storage full";
  svg.appendChild(legend);

  const pin = mk("g", { class: "truck-pin" });
  pin.innerHTML =
    `<circle class="halo" r="16" stroke-width="3"/>` +
    `<circle r="11" fill="#fff"/>` +
    `<rect x="-6" y="-4" width="8" height="7" rx="1" fill="#000"/><rect x="2" y="-2" width="4" height="5" rx="1" fill="#000"/>`;
  svg.appendChild(pin);

  return { path: bg, done, pin, len };
}

function buildEvents(points, trace, trip) {
  const f = trace.facts || {};
  const events = [
    {
      minute: 0,
      kind: "",
      text: `Leaves ${trip.from} for ${trip.to}${trip.customer ? ` carrying ${trip.customer}'s cargo` : ""}. Safe range ${PERMITTED_MIN_C}–${PERMITTED_MAX_C} °C.`,
    },
  ];
  const fault = points.find((p) => (p.fault_codes || []).length);
  if (fault) {
    events.push({
      minute: fault.minute,
      kind: "fault",
      text: `The cooling unit logs fault code ${fault.fault_codes.join(", ")}. The cargo is still ${fault.cargo_temp_c.toFixed(1)} °C — inside the safe range.`,
    });
  }
  const rec = parseRecommendation(trace);
  if (f.detected_at_minute != null) {
    const p = points.find((q) => q.minute === f.detected_at_minute);
    events.push({
      minute: f.detected_at_minute,
      kind: "warn",
      text:
        `AxonFDE warns: on its current trend this load will leave the safe range` +
        (p ? ` (now ${p.cargo_temp_c.toFixed(1)} °C)` : "") +
        `. ${rec ? `It recommends: ${rec}. ` : ""}A fleet manager is asked to approve.`,
    });
  }
  if (f.baseline_alarm_minute != null) {
    events.push({
      minute: f.baseline_alarm_minute,
      kind: "alarm",
      text:
        `The standard temperature alarm finally fires — the cargo is already above ${PERMITTED_MAX_C} °C.` +
        (f.detected_at_minute != null ? ` AxonFDE had warned ${f.baseline_alarm_minute - f.detected_at_minute} minutes earlier.` : ""),
    });
  }
  return events.sort((a, b) => a.minute - b.minute);
}

function startReplay(points, trace) {
  const trip = parseTrip(trace);
  const sites = parseSites(trace);
  const map = drawMap(trip, sites);
  const events = buildEvents(points, trace, trip);
  const f = trace.facts || {};
  const last = points[points.length - 1].minute;

  el("#replay-sub").textContent =
    `Shipment ${trace.shipment_id}, truck ${trace.vehicle_id}: ${trip.from} to ${trip.to}` +
    `${trip.customer ? ` for ${trip.customer}` : ""}. The cooling unit's compressor is slowly failing. ` +
    `Press play and watch what each system notices, and when. (A simulated trip; the readings are exactly what the sensors reported.)`;

  const scrub = el("#r-scrub");
  scrub.max = String(last);
  const playBtn = el("#r-play");
  const speedSel = el("#r-speed");
  const feed = el("#feed");

  let minute = 0;
  let playing = false;
  let lastT = 0;
  let shownEvents = -1;

  const byMinute = new Map(points.map((p) => [p.minute, p]));

  function render() {
    const m = Math.min(last, Math.floor(minute));
    const p = byMinute.get(m) ?? points[0];

    el("#g-min").textContent = `${m} min`;
    el("#g-temp").textContent = `${p.cargo_temp_c.toFixed(1)} °C`;
    el("#g-rpm").textContent = `${Math.round(p.compressor_rpm)} rpm`;
    el("#g-amb").textContent = `${p.ambient_temp_c.toFixed(1)} °C`;
    el("#g-codes").textContent = (p.fault_codes || []).length ? p.fault_codes.join(", ") : "none";

    /* The bar spans 0-12 °C with the safe range marked. */
    const span = 12;
    const bar = el("#g-temp-bar");
    bar.style.width = `${Math.max(0, Math.min(100, (p.cargo_temp_c / span) * 100))}%`;
    bar.classList.toggle("hot", p.cargo_temp_c > PERMITTED_MAX_C);
    el(".lim.lo").style.left = `${(PERMITTED_MIN_C / span) * 100}%`;
    el(".lim.hi").style.left = `${(PERMITTED_MAX_C / span) * 100}%`;

    const state =
      f.baseline_alarm_minute != null && m >= f.baseline_alarm_minute
        ? "alarm"
        : f.detected_at_minute != null && m >= f.detected_at_minute
        ? "warn"
        : "ok";
    const status = el("#g-status");
    status.className = `status ${state}`;
    el("#g-status-t").textContent =
      state === "alarm"
        ? "Standard alarm: cargo out of range"
        : state === "warn"
        ? "AxonFDE warning · standard alarm silent"
        : "All readings in range · no alert";

    if (map.len) {
      const pt = map.path.getPointAtLength((m / last) * map.len);
      map.pin.setAttribute("transform", `translate(${pt.x.toFixed(1)} ${pt.y.toFixed(1)})`);
      map.done.setAttribute("stroke-dasharray", `${((m / last) * map.len).toFixed(1)} ${map.len.toFixed(1)}`);
    }
    map.pin.setAttribute("class", `truck-pin ${state === "ok" ? "" : state}`);

    const visible = events.filter((e) => e.minute <= m);
    if (visible.length !== shownEvents) {
      shownEvents = visible.length;
      feed.innerHTML = "";
      for (const e of visible.slice().reverse()) {
        const li = document.createElement("li");
        li.className = e.kind;
        li.innerHTML = `<span class="t">min ${e.minute}</span><span></span>`;
        li.lastChild.textContent = e.text;
        feed.appendChild(li);
      }
    }

    scrub.value = String(m);
    setChartPlayhead(p);
  }

  function frame(t) {
    if (!playing) return;
    const dt = lastT ? (t - lastT) / 1000 : 0;
    lastT = t;
    minute += dt * Number(speedSel.value);
    if (minute >= last) {
      minute = last;
      setPlaying(false);
    }
    render();
    if (playing) requestAnimationFrame(frame);
  }

  function setPlaying(on) {
    playing = on;
    playBtn.textContent = on ? "❚❚ Pause" : minute >= last ? "▶ Play again" : "▶ Play";
    if (on) {
      if (minute >= last) minute = 0;
      lastT = 0;
      requestAnimationFrame(frame);
    }
  }

  playBtn.onclick = () => setPlaying(!playing);
  el("#r-restart").onclick = () => {
    minute = 0;
    shownEvents = -1;
    render();
    setPlaying(true);
  };
  scrub.oninput = () => {
    minute = Number(scrub.value);
    setPlaying(false);
    render();
  };

  if (REDUCED_MOTION || typeof IntersectionObserver !== "function") {
    /* No autoplay: show the moment that matters. */
    minute = f.baseline_alarm_minute ?? last;
    render();
    return;
  }
  render();
  const io = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        io.disconnect();
        setPlaying(true);
      }
    },
    { threshold: 0.35 }
  );
  io.observe(el(".replay"));
}

/* ── Why temperature alone is not enough ──────────────────────────── */

/* The window claims.md computes its C4 figures over: the 30 readings *ending*
 * at minute 100, i.e. minutes 71-100 inclusive.
 *
 * The boundary convention is not a detail. An inclusive 70-100 window is 31
 * readings and yields -8.8 rpm/min where the register records -9.2, for the
 * same scenario and the same stated "30-minute window". A reader comparing
 * this page against `claims.md` would find them disagreeing with no way to
 * tell which was wrong. The slopes below are recomputed from served data
 * rather than copied, so the window is the only thing that could silently
 * drift from the register - which is why it is spelled out here. */
const SLOPE_AT = 100;
const SLOPE_WINDOW = 30;
const SLOPE_FROM = SLOPE_AT - SLOPE_WINDOW + 1;
const SLOPE_TO = SLOPE_AT;

const COMPARED = [
  {
    id: "compressor_degradation_pharma_01",
    verdict: "Really warming",
    kind: "real",
    read: "The cooling unit is slowing down while the cargo warms: it is losing the fight. This load needs help.",
  },
  {
    id: "sensor_drift_pharma_01",
    verdict: "Fine — the sensor is lying",
    kind: "lying",
    read: "The cooling unit is working harder while the reading races upward — physically impossible if the reading were true. The thermometer is faulty, not the cargo.",
  },
];

/* Least squares over the window, not the difference between its endpoints.
 *
 * The first version subtracted endpoints and produced −8.7 rpm/min where
 * claims.md records −9.2 for the same scenario and window. Both are "the
 * slope", and a reader comparing the screen against the register would find
 * them disagreeing with no way to tell which was wrong. A fit is also what
 * `risk/features.py` uses, so the page now computes it the way the system
 * does rather than a cheaper way that looks the same. */
function slope(points, key) {
  const window = points.slice(SLOPE_FROM, SLOPE_TO + 1);
  if (window.length < 2) return null;

  const n = window.length;
  const meanX = (SLOPE_FROM + SLOPE_TO) / 2;
  const meanY = window.reduce((sum, p) => sum + p[key], 0) / n;

  let num = 0;
  let den = 0;
  window.forEach((p, i) => {
    const dx = SLOPE_FROM + i - meanX;
    num += dx * (p[key] - meanY);
    den += dx * dx;
  });
  return den === 0 ? null : num / den;
}

function sparkline(points, key, colour) {
  const W = 260;
  const H = 70;
  const pad = 6;
  const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" });

  const vals = points.map((p) => p[key]);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const span = hi - lo || 1;
  const x = (i) => pad + (i / (points.length - 1)) * (W - 2 * pad);
  const y = (v) => pad + (1 - (v - lo) / span) * (H - 2 * pad);

  /* The window the quoted slope is measured over, shaded so the number and
   * the picture are visibly about the same stretch of the run. */
  svg.appendChild(
    mk("rect", {
      x: x(SLOPE_FROM), y: pad,
      width: Math.max(1, x(SLOPE_TO) - x(SLOPE_FROM)), height: H - 2 * pad,
      fill: "rgba(255,255,255,.08)",
    })
  );
  svg.appendChild(
    mk("path", {
      d: points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`).join(" "),
      fill: "none", stroke: colour, "stroke-width": 1.8, "stroke-linejoin": "round",
      "vector-effect": "non-scaling-stroke",
    })
  );
  return svg;
}

function renderComparison(loaded) {
  const host = el("#compare");
  host.innerHTML = "";

  for (const { id, verdict, kind, read, points } of loaded) {
    const card = document.createElement("div");
    card.className = `cmp ${kind}`;

    const v = document.createElement("p");
    v.className = "cmp-verdict";
    v.textContent = verdict;
    const name = document.createElement("div");
    name.className = "cmp-name";
    name.textContent = `scenario ${id}`;

    const codes = [...new Set(points.flatMap((p) => p.fault_codes))];
    const badge = document.createElement("span");
    badge.className = `cmp-codes${codes.length ? " present" : ""}`;
    badge.textContent = codes.length ? `unit reports fault ${codes.join(", ")}` : "no fault code reported";

    const charts = document.createElement("div");
    charts.className = "cmp-charts";
    for (const [key, colour, unit, dp, title] of [
      ["cargo_temp_c", "#ffffff", "°C / min", 3, "Cargo temperature"],
      ["compressor_rpm", "var(--cyan)", "rpm / min", 1, "Cooling unit speed"],
    ]) {
      const value = slope(points, key);
      const cell = document.createElement("div");
      cell.appendChild(sparkline(points, key, colour));
      const cap = document.createElement("div");
      cap.className = "cmp-cap";
      cap.innerHTML = `<i style="background:${colour}"></i>${title} <b>${
        value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(dp)} ${unit}`
      }</b>`;
      cell.appendChild(cap);
      charts.appendChild(cell);
    }

    const r = document.createElement("p");
    r.className = "cmp-read";
    r.textContent = read;

    card.append(v, name, badge, charts, r);
    host.appendChild(card);
  }

  el("#compare-note").innerHTML =
    `<strong>Temperature alone can't tell these apart</strong> — over minutes ${SLOPE_FROM}–${SLOPE_TO} ` +
    `the faulty sensor actually climbs <em>faster</em>. What separates them is how the cooling unit is ` +
    `responding (the shaded window is where the rates are measured). The numbers are computed on this page ` +
    `from the served readings and match the published register, <code>claims.md</code>, because they are ` +
    `the same calculation rather than copied values.`;
}

/* ── Timeline ─────────────────────────────────────────────────────── */

const GLYPH = { good: "✓", refused: "⛔", note: "", say: "" };

function renderTimeline(trace) {
  const list = el("#timeline");
  list.innerHTML = "";

  for (const step of trace.steps) {
    if (!step.title) continue;
    const li = document.createElement("li");
    li.className = "tstep";
    li.dataset.n = String(step.number);
    if (step.lines.some((l) => l.kind === "refused")) li.classList.add("has-refusal");

    const h = document.createElement("h3");
    h.textContent = step.title;
    li.appendChild(h);

    const plain = PLAIN_STEPS[step.title];
    if (plain) {
      const p = document.createElement("p");
      p.className = "plain";
      p.textContent = plain;
      li.appendChild(p);
    }

    const line = (l) => {
      const row = document.createElement("div");
      row.className = `tline ${l.kind}`;
      const glyph = document.createElement("span");
      glyph.className = "glyph";
      glyph.textContent = GLYPH[l.kind] ?? "";
      const text = document.createElement("span");
      text.textContent = stripAnsi(l.text);
      row.append(glyph, text);
      return row;
    };

    const lines = step.lines.filter((l) => l.text);
    for (const l of lines.filter((l) => l.kind === "good" || l.kind === "refused")) li.appendChild(line(l));
    const rest = lines.filter((l) => l.kind !== "good" && l.kind !== "refused");
    if (rest.length) {
      const det = document.createElement("details");
      const sum = document.createElement("summary");
      sum.textContent = "Technical detail";
      det.appendChild(sum);
      for (const l of rest) det.appendChild(line(l));
      li.appendChild(det);
    }
    list.appendChild(li);
  }

  el("#loop-sub").textContent =
    `${trace.total_steps} steps, recorded from the last full run of the real code against real databases, ` +
    `in ${trace.elapsed_seconds} seconds. The amber steps are where the system refused to go ahead — ` +
    `those refusals are the point.`;

  const f = trace.facts || {};
  if (f.detected_at_minute != null && f.baseline_alarm_minute != null) {
    el("#st-lead").innerHTML = `${f.baseline_alarm_minute - f.detected_at_minute}<small>min</small>`;
    el("#stats").hidden = false;
  }
}

/* ── Reveal on scroll ─────────────────────────────────────────────── */

function revealOnScroll() {
  if (REDUCED_MOTION || typeof IntersectionObserver !== "function") return;
  const targets = document.querySelectorAll(".section h2, .card, .stepcard, .claim, .cmp, .ai-card, .tstep");
  const io = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      }
    },
    { threshold: 0.12 }
  );
  const fold = window.innerHeight || 0;
  targets.forEach((t, i) => {
    /* Only what the reader has not reached yet; nothing on screen blinks out. */
    if (t.getBoundingClientRect().top < fold) return;
    t.classList.add("reveal");
    t.style.transitionDelay = `${(i % 4) * 70}ms`;
    io.observe(t);
  });
}

/* ── Boot ─────────────────────────────────────────────────────────── */

(async function main() {
  el("#env-range").textContent = `${PERMITTED_MIN_C.toFixed(0)} and ${PERMITTED_MAX_C.toFixed(0)} °C`;

  /* Settled, not all-or-nothing: a missing demo trace must not blank the
   * claim cards, and vice versa. Each section reports its own absence. */
  const [claims, trace] = await Promise.allSettled([
    getJSON(`${API}/control/claims`),
    getJSON(`${API}/control/trace`),
  ]);

  if (claims.status === "fulfilled") renderClaims(claims.value);
  else showEmpty(el("#claim-grid"), claims.reason);

  if (trace.status !== "fulfilled") {
    for (const sel of ["#timeline", "#legend", "#feed", "#hyps"]) showEmpty(el(sel), trace.reason);
    el("#replay-sub").textContent = "No recorded run to replay yet.";
    revealOnScroll();
    return;
  }

  renderTimeline(trace.value);
  renderShowcase(trace.value.llm_showcase);

  try {
    const telemetry = await getJSON(`${API}/control/telemetry/${trace.value.scenario_id}`);
    renderChart(telemetry.points, detectionMarks(trace.value));
    startHero(telemetry.points, trace.value.facts || {});
    startReplay(telemetry.points, trace.value);
  } catch (err) {
    showEmpty(el("#legend"), err);
    showEmpty(el("#feed"), err);
  }

  try {
    const loaded = await Promise.all(
      COMPARED.map(async (c) => ({
        ...c,
        points: (await getJSON(`${API}/control/telemetry/${c.id}`)).points,
      }))
    );
    renderComparison(loaded);
  } catch (err) {
    showEmpty(el("#compare"), err);
  }

  revealOnScroll();
})();
