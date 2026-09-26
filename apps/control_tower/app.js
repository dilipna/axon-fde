/* Control tower.
 *
 * Reads three endpoints and renders them. There is deliberately no fallback to
 * sample data anywhere in this file: if a fetch fails the panel says what is
 * missing and which command produces it. A dashboard that looks the same
 * whether or not the system ran is worse than one that is plainly empty.
 */

const API = "/api/v1";

const PERMITTED_MIN_C = 2.0;
const PERMITTED_MAX_C = 8.0;

/* Which claims get a card, in the order they are shown, and how to phrase
 * each one. Held here rather than derived from the run so that a claim the
 * harness starts reporting does not silently appear in the UI unlabelled. */
const CLAIM_CARDS = [
  {
    id: "C1",
    label: "median lead time over the threshold alarm",
    format: (r) => `${fmt(r.value)} min`,
    /* C1 may never be shown alone. claims.md calls the unpaired number a
     * misuse, so the card refuses to render without its companion. */
    pair: (r) => {
      const far = r.companions?.false_alarm_rate;
      const base = r.companions?.baseline_false_alarm_rate;
      if (far === undefined) return null;
      return `at a ${pct(far)} false-alarm rate${
        base === undefined ? "" : ` — the threshold alarm's is ${pct(base)}`
      }`;
    },
  },
  {
    id: "C6",
    label: "recall against seeded cross-source conflicts",
    format: (r) => fmt(r.value, 2),
    pair: (r) =>
      r.companions?.precision === undefined
        ? null
        : `precision ${fmt(r.companions.precision, 2)} over ${fmt(
            r.companions.negative_cases
          )} near-miss negatives`,
  },
  {
    id: "C7",
    label: "unauthorised actions across the role × action matrix",
    format: (r) => fmt(r.value),
    zeroIsGood: true,
  },
  {
    id: "C8",
    label: "prohibited statements reaching the driver",
    format: (r) => fmt(r.value),
    zeroIsGood: true,
  },
];

const fmt = (n, dp = 0) =>
  n === undefined || n === null ? "—" : Number(n).toFixed(dp);
const pct = (n) => `${(Number(n) * 100).toFixed(0)}%`;
const el = (sel) => document.querySelector(sel);

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

/* ── Claims ───────────────────────────────────────────────────────── */

function renderClaims(run) {
  const grid = el("#claim-grid");
  grid.innerHTML = "";

  const byId = Object.fromEntries(
    (run.results || []).map((r) => [r.claim_id, r])
  );

  for (const card of CLAIM_CARDS) {
    const r = byId[card.id];
    if (!r || r.status !== "MEASURED") continue;

    const node = document.createElement("div");
    node.className = "claim";

    const id = document.createElement("div");
    id.className = "claim-id";
    id.textContent = `${card.id} · ${r.kind === "safety_gate" ? "SAFETY GATE" : "QUALITY"}`;

    const value = document.createElement("div");
    value.className = "claim-value";
    if (card.zeroIsGood && Number(r.value) === 0) value.classList.add("zero");
    value.textContent = card.format(r);

    const label = document.createElement("div");
    label.className = "claim-label";
    label.textContent = card.label;

    const cases = document.createElement("div");
    cases.className = "claim-cases";
    cases.textContent = `${r.cases}/${r.required_cases} ${r.case_unit}`;

    node.append(id, value, label, cases);

    const pair = card.pair?.(r);
    if (pair) {
      const p = document.createElement("div");
      p.className = "claim-pair";
      p.textContent = pair;
      node.appendChild(p);
    }
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
      `On <strong>${pct(early)}</strong> of breach scenarios the predictive detector fired ` +
      `<strong>before the causal fault started</strong> — in hot ambient the cargo genuinely ` +
      `climbs while the unit settles, and linear extrapolation cannot tell that curve from an ` +
      `excursion. Restricted to alerts that followed their fault the median is ` +
      `<strong>${fmt(after)} min</strong>. Both are published; the correction costs the claim ` +
      `time rather than buying it any.`;
  }

  const p = run.provenance || {};
  el("#run-id").textContent = p.run_id || "—";
  const bar = el("#provenance");
  bar.innerHTML = "";
  const pills = [
    `run ${p.run_id ?? "—"}`,
    `code ${p.git_sha ?? "—"}`,
    `pack ${p.pack_version ?? "—"}`,
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
}

/* ── Chart ────────────────────────────────────────────────────────── */

const SVG = "http://www.w3.org/2000/svg";
const mk = (name, attrs) => {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
};

function renderChart(points, marks) {
  const svg = el("#chart");
  svg.innerHTML = "";
  if (!points.length) return;

  const W = svg.clientWidth || 900;
  const H = 340;
  const pad = { t: 22, r: 18, b: 30, l: 44 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const temps = points.map((p) => p.cargo_temp_c);
  const lo = Math.min(PERMITTED_MIN_C, ...temps) - 1;
  const hi = Math.max(PERMITTED_MAX_C, ...temps) + 1;
  const maxMin = points[points.length - 1].minute;

  const x = (m) => pad.l + (m / maxMin) * (W - pad.l - pad.r);
  const y = (t) => pad.t + (1 - (t - lo) / (hi - lo)) * (H - pad.t - pad.b);

  /* Gradient under the line and a glow filter. Decoration only: the line and
   * the marks carry the data. */
  const defs = mk("defs", {});
  defs.innerHTML =
    `<linearGradient id="area" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0" stop-color="#7c5cf0" stop-opacity=".38"/>` +
    `<stop offset="1" stop-color="#7c5cf0" stop-opacity="0"/></linearGradient>` +
    `<filter id="glow" x="-5%" y="-20%" width="110%" height="140%">` +
    `<feGaussianBlur stdDeviation="3" result="b"/>` +
    `<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>`;
  svg.appendChild(defs);

  /* Recessive horizontal grid. */
  const step = hi - lo > 14 ? 4 : 2;
  for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) {
    svg.appendChild(
      mk("line", { x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t), stroke: "rgba(148,170,220,.08)" })
    );
  }

  /* The permitted envelope as a band, so "in spec" is a region rather than a
   * number the viewer has to hold in their head. */
  svg.appendChild(
    mk("rect", {
      x: pad.l,
      y: y(PERMITTED_MAX_C),
      width: W - pad.l - pad.r,
      height: Math.max(0, y(PERMITTED_MIN_C) - y(PERMITTED_MAX_C)),
      fill: "var(--envelope)",
      stroke: "rgba(159,176,208,.4)",
      "stroke-dasharray": "2 4",
    })
  );

  for (const t of [PERMITTED_MIN_C, PERMITTED_MAX_C]) {
    const label = mk("text", {
      x: 8,
      y: y(t) + 4,
      fill: "var(--text-faint)",
      "font-size": "11",
      "font-family": "var(--mono)",
    });
    label.textContent = `${t}°`;
    svg.appendChild(label);
  }

  const line = points
    .map((p, i) => `${i ? "L" : "M"}${x(p.minute).toFixed(1)},${y(p.cargo_temp_c).toFixed(1)}`)
    .join(" ");
  const base = y(lo);
  svg.appendChild(
    mk("path", {
      d: `${line} L${x(maxMin).toFixed(1)},${base} L${x(points[0].minute).toFixed(1)},${base} Z`,
      fill: "url(#area)",
    })
  );
  const trace = mk("path", {
    d: line,
    fill: "none",
    stroke: "var(--violet-ink)",
    "stroke-width": 2,
    "stroke-linejoin": "round",
    filter: "url(#glow)",
    class: "draw",
  });
  svg.appendChild(trace);
  /* The draw-in animation needs the path length; set it once the node is live. */
  try {
    trace.style.setProperty("--len", String(Math.ceil(trace.getTotalLength())));
  } catch {
    trace.removeAttribute("class"); /* no layout engine: draw it statically */
  }

  for (const m of marks) {
    if (m.minute === null || m.minute === undefined) continue;
    svg.appendChild(
      mk("line", {
        x1: x(m.minute),
        x2: x(m.minute),
        y1: pad.t,
        y2: H - pad.b,
        stroke: m.colour,
        "stroke-width": 1.6,
        "stroke-dasharray": m.dash ?? "0",
      })
    );
    /* Direct label, in ink rather than the series colour, on a plate so it
     * stays legible over the trace. */
    const text = `${m.label} ${m.minute}′`;
    const w = text.length * 6.7 + 12;
    const left = x(m.minute) + w + 6 > W - pad.r;
    const lx = left ? x(m.minute) - w - 5 : x(m.minute) + 5;
    svg.appendChild(
      mk("rect", { x: lx, y: pad.t - 2, width: w, height: 18, rx: 5, fill: "rgba(7,11,20,.88)", stroke: m.colour, "stroke-opacity": 0.7 })
    );
    const label = mk("text", {
      x: lx + 6,
      y: pad.t + 11,
      fill: "var(--text)",
      "font-size": "11",
      "font-family": "var(--mono)",
    });
    label.textContent = text;
    svg.appendChild(label);
  }

  const axis = mk("text", {
    x: W - pad.r,
    y: H - 8,
    fill: "var(--text-faint)",
    "font-size": "11",
    "text-anchor": "end",
    "font-family": "var(--mono)",
  });
  axis.textContent = "minutes since departure";
  svg.appendChild(axis);

  el("#legend").innerHTML = [
    `<span><i class="swatch" style="background:var(--violet-ink)"></i>reported cargo temperature</span>`,
    `<span><i class="swatch box" style="background:var(--envelope);border:1px dashed rgba(159,176,208,.6)"></i>permitted envelope (${PERMITTED_MIN_C}–${PERMITTED_MAX_C}°C, per the signed Bill of Lading)</span>`,
    ...marks
      .filter((m) => m.minute !== null && m.minute !== undefined)
      .map(
        (m) =>
          `<span><i class="swatch${m.dash ? " dash" : ""}" style="background:${m.colour};border-color:${m.colour}"></i>${m.legend}</span>`
      ),
  ].join("");

  attachCrosshair(svg, points, marks, { x, y, pad, W, H, maxMin });
}

/* Crosshair + tooltip: hover reads out the exact reported value and what the
 * system had concluded by then. Values come from the served points; nothing
 * is interpolated for display. */
function attachCrosshair(svg, points, marks, g) {
  const wrap = svg.parentElement;
  const old = wrap.querySelector(".tip");
  if (old) old.remove();
  const tip = document.createElement("div");
  tip.className = "tip";
  wrap.appendChild(tip);

  const cross = mk("line", { y1: g.pad.t, y2: g.H - g.pad.b, stroke: "rgba(232,238,252,.5)", "stroke-width": 1, visibility: "hidden" });
  const dot = mk("circle", { r: 4.5, fill: "var(--violet-ink)", stroke: "#070b14", "stroke-width": 2, visibility: "hidden" });
  svg.append(cross, dot);

  const detect = marks.find((m) => m.label === "AxonFDE")?.minute;
  const alarm = marks.find((m) => m.label !== "AxonFDE")?.minute;

  svg.onmousemove = (ev) => {
    const box = svg.getBoundingClientRect();
    const vx = ((ev.clientX - box.left) / box.width) * g.W;
    const minute = Math.max(0, Math.min(g.maxMin, ((vx - g.pad.l) / (g.W - g.pad.l - g.pad.r)) * g.maxMin));
    const p = points.reduce((best, q) => (Math.abs(q.minute - minute) < Math.abs(best.minute - minute) ? q : best));
    const px = g.x(p.minute);
    cross.setAttribute("x1", px);
    cross.setAttribute("x2", px);
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px);
    dot.setAttribute("cy", g.y(p.cargo_temp_c));
    dot.setAttribute("visibility", "visible");

    const state =
      alarm != null && p.minute >= alarm
        ? "threshold alarm has fired"
        : detect != null && p.minute >= detect
        ? "AxonFDE has already flagged this"
        : "no alert yet";
    tip.innerHTML =
      `<div class="t">minute ${p.minute}</div><div>reported <b>${p.cargo_temp_c.toFixed(2)}°C</b></div>` +
      `<div class="state">${state}</div>`;
    const tx = (px / g.W) * box.width;
    tip.style.left = `${Math.min(tx + 14, box.width - 170)}px`;
    tip.style.top = `${Math.max(0, (g.y(p.cargo_temp_c) / g.H) * box.height - 70)}px`;
    tip.classList.add("on");
  };
  svg.onmouseleave = () => {
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    tip.classList.remove("on");
  };
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
    verdict: "the cargo really is warming",
    kind: "real",
  },
  {
    id: "sensor_drift_pharma_01",
    verdict: "the cargo is fine — the sensor is lying",
    kind: "lying",
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
      x: x(SLOPE_FROM),
      y: pad,
      width: Math.max(1, x(SLOPE_TO) - x(SLOPE_FROM)),
      height: H - 2 * pad,
      fill: "rgba(159,176,208,.10)",
    })
  );
  svg.appendChild(
    mk("path", {
      d: points
        .map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[key]).toFixed(1)}`)
        .join(" "),
      fill: "none",
      stroke: colour,
      "stroke-width": 1.8,
      "stroke-linejoin": "round",
    })
  );
  return svg;
}

function renderComparison(loaded) {
  const host = el("#compare");
  host.innerHTML = "";

  for (const { id, verdict, kind, points } of loaded) {
    const row = document.createElement("div");
    row.className = "cmp-row";

    const head = document.createElement("div");
    head.className = "cmp-head";
    const name = document.createElement("span");
    name.className = "cmp-name";
    name.textContent = id;
    const verdictEl = document.createElement("span");
    verdictEl.className = `cmp-verdict ${kind}`;
    verdictEl.textContent = `— ${verdict}`;

    const codes = [...new Set(points.flatMap((p) => p.fault_codes))];
    const badge = document.createElement("span");
    badge.className = `cmp-codes${codes.length ? " present" : ""}`;
    badge.textContent = codes.length ? `unit reports ${codes.join(", ")}` : "no fault code";

    head.append(name, verdictEl, badge);

    const charts = document.createElement("div");
    charts.className = "cmp-charts";

    const tSlope = slope(points, "cargo_temp_c");
    const rSlope = slope(points, "compressor_rpm");

    for (const [key, colour, unit, value] of [
      ["cargo_temp_c", "var(--violet-ink)", "°C/min", tSlope],
      ["compressor_rpm", "var(--cyan-ink)", "rpm/min", rSlope],
    ]) {
      const cell = document.createElement("div");
      cell.appendChild(sparkline(points, key, colour));
      const cap = document.createElement("div");
      cap.className = "cmp-cap";
      cap.innerHTML = `<i style="background:${colour}"></i>${key} &nbsp; <b>${value >= 0 ? "+" : ""}${value.toFixed(
        key === "cargo_temp_c" ? 3 : 1
      )} ${unit}</b>`;
      cell.appendChild(cap);
      charts.appendChild(cell);
    }

    row.append(head, charts);
    host.appendChild(row);
  }

  el("#compare-note").innerHTML =
    `Both temperature traces climb, and over minutes ${SLOPE_FROM}–${SLOPE_TO} the <em>lying</em> ` +
    `sensor climbs faster. Temperature alone cannot separate them. What can is the ` +
    `<strong>compressor response</strong>: a unit winding <em>down</em> while cargo warms is ` +
    `losing the fight; one winding <em>up</em> while the reading races is physically incoherent, ` +
    `and the instrument is what is wrong. The absence of a fault code says the same thing. ` +
    `<strong>Both are single-modality telemetry.</strong> That is why the multimodal ablation's ` +
    `baseline arm must include compressor response — otherwise a photograph gets credited with a ` +
    `discrimination telemetry already made, and the claim is inflated. Recorded in ` +
    `<code>claims.md</code> before the ablation was built.`;
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

    for (const line of step.lines) {
      if (!line.text) continue;
      const row = document.createElement("div");
      row.className = `tline ${line.kind}`;
      const glyph = document.createElement("span");
      glyph.className = "glyph";
      glyph.textContent = GLYPH[line.kind] ?? "";
      const text = document.createElement("span");
      text.textContent = line.text;
      row.append(glyph, text);
      li.appendChild(row);
    }
    list.appendChild(li);
  }

  const f = trace.facts || {};
  const lead =
    f.lead_time_minutes == null
      ? ""
      : ` · ${f.lead_time_minutes} min of warning on this shipment`;
  /* Hero readouts: the same recorded facts the chart marks read. The warning
   * shown is this shipment's, labelled as such - C1's fleet median is only ever
   * shown with its false-alarm rate, in the claims panel. */
  if (f.detected_at_minute != null && f.baseline_alarm_minute != null) {
    el("#ro-detect").innerHTML = `${f.detected_at_minute}<small>min</small>`;
    el("#ro-alarm").innerHTML = `${f.baseline_alarm_minute}<small>min</small>`;
    el("#ro-lead").innerHTML = `${f.baseline_alarm_minute - f.detected_at_minute}<small>min</small>`;
    el("#readouts").hidden = false;
  }
  el("#scenario-hint").textContent =
    `${trace.scenario_id} · shipment ${trace.shipment_id} · vehicle ${trace.vehicle_id}` +
    `${lead} · replayed in ${trace.elapsed_seconds}s`;
  el("#arm-label").textContent = `${trace.arm.replace("_", "-")} arm · model: ${trace.model_id}`;
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
      minute: Number(f.detected_at_minute),
      colour: "var(--cyan)",
      label: "AxonFDE",
      legend: "predictive detection",
    });
  }
  if (f.baseline_alarm_minute != null) {
    marks.push({
      minute: Number(f.baseline_alarm_minute),
      colour: "var(--rose)",
      dash: "5 4",
      label: "threshold alarm",
      legend: "threshold alarm — the cargo is already out of spec",
    });
  }
  return marks;
}

/* ── Boot ─────────────────────────────────────────────────────────── */

(async function main() {
  /* Settled, not all-or-nothing: a missing demo trace must not blank the
   * claim cards, and vice versa. Each panel reports its own absence. */
  const [claims, trace] = await Promise.allSettled([
    getJSON(`${API}/control/claims`),
    getJSON(`${API}/control/trace`),
  ]);

  if (claims.status === "fulfilled") renderClaims(claims.value);
  else showEmpty(el("#claim-grid"), claims.reason);

  if (trace.status !== "fulfilled") {
    showEmpty(el("#timeline"), trace.reason);
    showEmpty(el("#legend"), trace.reason);
    return;
  }

  renderTimeline(trace.value);

  try {
    const telemetry = await getJSON(
      `${API}/control/telemetry/${trace.value.scenario_id}`
    );
    renderChart(telemetry.points, detectionMarks(trace.value));
  } catch (err) {
    showEmpty(el("#legend"), err);
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
})();
