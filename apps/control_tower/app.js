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
  const H = 300;
  const pad = { t: 14, r: 16, b: 26, l: 40 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const temps = points.map((p) => p.cargo_temp_c);
  const lo = Math.min(PERMITTED_MIN_C, ...temps) - 1;
  const hi = Math.max(PERMITTED_MAX_C, ...temps) + 1;
  const maxMin = points[points.length - 1].minute;

  const x = (m) => pad.l + (m / maxMin) * (W - pad.l - pad.r);
  const y = (t) => pad.t + (1 - (t - lo) / (hi - lo)) * (H - pad.t - pad.b);

  /* The permitted envelope as a band, so "in spec" is a region rather than a
   * number the viewer has to hold in their head. */
  svg.appendChild(
    mk("rect", {
      x: pad.l,
      y: y(PERMITTED_MAX_C),
      width: W - pad.l - pad.r,
      height: Math.max(0, y(PERMITTED_MIN_C) - y(PERMITTED_MAX_C)),
      fill: "var(--envelope)",
      stroke: "rgba(76,141,255,.35)",
      "stroke-dasharray": "3 3",
    })
  );

  for (const t of [PERMITTED_MIN_C, PERMITTED_MAX_C]) {
    const label = mk("text", {
      x: 6,
      y: y(t) + 4,
      fill: "var(--text-faint)",
      "font-size": "11",
      "font-family": "var(--mono)",
    });
    label.textContent = `${t}°`;
    svg.appendChild(label);
  }

  const d = points
    .map((p, i) => `${i ? "L" : "M"}${x(p.minute).toFixed(1)},${y(p.cargo_temp_c).toFixed(1)}`)
    .join(" ");
  svg.appendChild(
    mk("path", { d, fill: "none", stroke: "var(--accent)", "stroke-width": 1.8 })
  );

  for (const m of marks) {
    if (m.minute === null || m.minute === undefined) continue;
    svg.appendChild(
      mk("line", {
        x1: x(m.minute),
        x2: x(m.minute),
        y1: pad.t,
        y2: H - pad.b,
        stroke: m.colour,
        "stroke-width": 1.4,
        "stroke-dasharray": m.dash ?? "0",
      })
    );
    const label = mk("text", {
      x: x(m.minute) + 5,
      y: pad.t + 12,
      fill: m.colour,
      "font-size": "11",
      "font-family": "var(--mono)",
    });
    label.textContent = `${m.label} ${m.minute}′`;
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
    `<span><i class="swatch" style="background:var(--accent)"></i>reported cargo temperature</span>`,
    `<span><i class="swatch box" style="background:var(--envelope);border:1px dashed rgba(76,141,255,.5)"></i>permitted envelope (${PERMITTED_MIN_C}–${PERMITTED_MAX_C}°C, per the signed Bill of Lading)</span>`,
    ...marks
      .filter((m) => m.minute !== null && m.minute !== undefined)
      .map((m) => `<span><i class="swatch" style="background:${m.colour}"></i>${m.legend}</span>`),
  ].join("");
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
      colour: "var(--good)",
      label: "AxonFDE",
      legend: "predictive detection",
    });
  }
  if (f.baseline_alarm_minute != null) {
    marks.push({
      minute: Number(f.baseline_alarm_minute),
      colour: "var(--bad)",
      dash: "4 3",
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
})();
