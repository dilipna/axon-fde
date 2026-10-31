/* Pure workbench contracts. The public UI never authorizes or executes a carrier action. */
export async function sha256(text) {
  const bytes = new TextEncoder().encode(text.replace(/\r\n/g, "\n"));
  return Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (b) => b.toString(16).padStart(2, "0"),
  ).join("");
}
export async function verifyBundle(manifest, text) {
  if (
    text.length > 2_000_000 ||
    manifest?.schema !== 1 ||
    manifest.algorithm !== "sha256-normalized-utf8-lf" ||
    manifest.payload !== "workbench-data.json" ||
    !/^[a-f0-9]{64}$/.test(manifest.payload_sha256 || "")
  )
    throw Error("Invalid evidence manifest");
  if ((await sha256(text)) !== manifest.payload_sha256)
    throw Error("Evidence fingerprint mismatch");
  const data = JSON.parse(text);
  if (
    data.schema !== 1 ||
    !Array.isArray(data.trace?.steps) ||
    data.trace.steps.length > 200 ||
    !Array.isArray(data.planning_policy) ||
    data.planning_policy.length > 32
  )
    throw Error("Unsupported recorded evidence");
  for (const key of ["compressor", "sensor"]) {
    const points = data.telemetry?.[key]?.points;
    if (
      !Array.isArray(points) ||
      !points.length ||
      points.length > 20000 ||
      points.some(
        (p) =>
          !Number.isFinite(p.minute) ||
          !Number.isFinite(p.cargo_temp_c) ||
          "true_cargo_temp_c" in p,
      )
    )
      throw Error("Invalid public telemetry");
  }
  if (
    !Array.isArray(data.verification?.after?.rows) ||
    data.verification.after.rows.length !== 24
  )
    throw Error("Missing frozen verifier evidence");
  for (const step of data.trace.steps)
    if (
      !Number.isInteger(step.number) ||
      typeof step.title !== "string" ||
      !Array.isArray(step.lines) ||
      step.lines.some(
        (l) => typeof l.text !== "string" || l.text.length > 16000,
      )
    )
      throw Error("Malformed trace record");
  return data;
}
export function rankPlanning(policy, probability, value, blocked = []) {
  if (
    !Number.isFinite(probability) ||
    probability < 0 ||
    probability > 1 ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > 10000000
  )
    throw Error(
      "Use a finite risk between 0 and 1 and a cargo value up to $10 million",
    );
  if (
    !Array.isArray(policy) ||
    !policy.length ||
    policy.length > 32 ||
    new Set(policy.map((c) => c.id)).size !== policy.length ||
    !policy.some((c) => c.id === "do_nothing")
  )
    throw Error("Invalid action catalogue");
  if (
    !Array.isArray(blocked) ||
    blocked.some((id) => !policy.some((c) => c.id === id))
  )
    throw Error("Unknown blocked option");
  const options = policy.map((cost) => {
    if (
      ![cost.direct_cost_usd, cost.delay_minutes, cost.risk_multiplier].every(
        Number.isFinite,
      ) ||
      cost.direct_cost_usd < 0 ||
      cost.delay_minutes < 0 ||
      cost.risk_multiplier < 0 ||
      cost.risk_multiplier > 1
    )
      throw Error("Invalid cost assumption");
    const delay = cost.delay_minutes * 2.5,
      loss = probability * cost.risk_multiplier * value;
    return {
      id: cost.id,
      feasible: cost.id === "do_nothing" || !blocked.includes(cost.id),
      ev: -(cost.direct_cost_usd + delay + loss),
      direct: cost.direct_cost_usd,
      delay,
      loss,
      basis: cost.basis,
    };
  });
  return options.sort(
    (a, b) =>
      Number(b.feasible) - Number(a.feasible) ||
      b.ev - a.ev ||
      Number(a.id !== "do_nothing") - Number(b.id !== "do_nothing") ||
      (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
}
export function answerRecorded(question, data, caseId = "compressor") {
  if (typeof question !== "string" || !question.trim() || question.length > 500)
    return {
      text: "Ask one question about this recording (up to 500 characters).",
      citations: [],
    };
  if (caseId !== "compressor")
    return {
      text: "This sensor study contains telemetry, not a recorded approval or intervention. It cannot establish which action was authorized or whether a real shipment recovered.",
      citations: ["telemetry.sensor"],
    };
  const q = question.toLowerCase();
  let ids = [];
  if (/\b(approv|stale|blocked|changed|signature)/.test(q))
    ids = [9, 10, 11, 12];
  else if (/\b(verif|outcome|recover|failed|audit)/.test(q)) ids = [13];
  else if (/\b(recommend|reroute|cost|option|decision|why)/.test(q))
    ids = [7, 8];
  else if (/\b(alarm|early|warn|lead|detect)/.test(q)) ids = [5];
  else if (/\b(contract|paper|document|envelope|limit)/.test(q)) ids = [3, 4];
  if (!ids.length)
    return {
      text: "The recording does not establish that. Try asking about the warning, chosen option, stale approval, contractual limit or final outcome.",
      citations: [],
    };
  const steps = data.trace.steps.filter((s) => ids.includes(s.number));
  return {
    text: steps
      .map(
        (s) =>
          `${s.title}\n${s.lines
            .map((l) => l.text)
            .filter(Boolean)
            .join("\n")}`,
      )
      .join("\n\n"),
    citations: steps.map((s) => `trace.steps[${s.number}]`),
  };
}
export function planningKey(
  { probability, value, blocked },
  sourceHash,
  caseId = "compressor",
) {
  return JSON.stringify({
    scope: "browser-only planning rehearsal",
    caseId,
    sourceHash,
    probability,
    value,
    blocked: [...blocked].sort(),
  });
}
export function makePacket({
  data,
  manifest,
  caseId,
  notes,
  planning,
  reviewKey,
}) {
  if (!["compressor", "sensor"].includes(caseId)) throw Error("Unknown study");
  const key = planningKey(planning, manifest.payload_sha256, caseId);
  return {
    schema: 1,
    scope:
      "Recorded synthetic study and browser-only planning rehearsal; not live fleet data, backend approval or an executed action.",
    exported_at: new Date().toISOString(),
    recorded_at: data.recorded_at,
    case_id: caseId,
    source_fingerprint: manifest.payload_sha256,
    sources: data.sources,
    notes: String(notes || "").slice(0, 10000),
    recorded_steps: caseId === "compressor" ? data.trace.steps : [],
    planning: {
      ...planning,
      policy: data.planning_policy,
      calibrated_probability: false,
      review_status:
        reviewKey === key
          ? "browser rehearsal reviewed"
          : "not reviewed or assumptions changed",
    },
    scope_limits: [
      "Cost policy parameters are assumptions, not measured savings.",
      "No intervention is authorized or executed in this website.",
      "One sensor does not prove physical truth.",
    ],
  };
}

export function makeBrief(packet) {
  const safe = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  return [
    "# AxonFDE shift brief",
    "",
    safe(packet.scope),
    "",
    "Recorded: " + safe(packet.recorded_at),
    "Study: " + safe(packet.case_id),
    "Source fingerprint: " + safe(packet.source_fingerprint),
    "",
    "## My notes",
    safe(packet.notes || "No notes added."),
    "",
    "## Planning rehearsal",
    "Risk assumption: " + safe(packet.planning.probability),
    "Cargo value assumption: " + safe(packet.planning.value),
    "Review: " + safe(packet.planning.review_status),
    "",
    "## Original record",
    ...packet.recorded_steps.flatMap((step) => [
      "",
      `### ${step.number}. ${safe(step.title)}`,
      ...step.lines.map((line) => safe(line.text)),
    ]),
    "",
    "## Limits",
    ...packet.scope_limits.map(safe),
  ].join("\n");
}
