// Browser-local, deterministic educational model. Not a fleet model or risk predictor.
export function createRun(seed = 42) {
  if (!Number.isInteger(seed) || seed < 1 || seed > 999999)
    throw Error("Seed must be an integer from 1 to 999999.");
  return {
    seed,
    minute: 0,
    sequence: 0,
    events: [],
    vehicles: ["Medicine delivery", "Fresh produce", "Frozen cargo"].map(
      (name, i) => ({
        id: `SIM-${101 + i}`,
        name,
        low: i === 2 ? -22 : 2,
        high: i === 2 ? -18 : 8,
        temp: i === 2 ? -20 : 5,
        ambient: 32,
        fault: "none",
        action: null,
        status: "Monitoring",
        hold: 0,
        proposal: null,
        readings: [],
      }),
    ),
  };
}
function event(run, v, text) {
  run.events.unshift({
    sequence: run.sequence,
    minute: run.minute,
    vehicle: v.id,
    text,
  });
  run.events.length = Math.min(run.events.length, 80);
}
export function injectFault(run, id, fault) {
  const v = run.vehicles.find((v) => v.id === id);
  if (!v || !["none", "cooling", "door", "sensor"].includes(fault))
    throw Error("Unknown vehicle or fault.");
  v.fault = fault;
  v.action = null;
  v.proposal = null;
  v.hold = 0;
  v.status = "Monitoring";
  event(run, v, `Fault setting: ${fault}. Previous proposal invalidated.`);
}
export function tick(run) {
  run.minute++;
  run.sequence++;
  for (const [i, v] of run.vehicles.entries()) {
    const old = v.temp,
      setpoint = (v.low + v.high) / 2;
    const noise = Math.sin((run.minute + run.seed + i * 31) * 1.7) * 0.025;
    // First-order thermal relaxation; distinct from the benchmark's Python model.
    const warming =
      v.fault === "cooling" ? 0.19 : v.fault === "door" ? 0.28 : 0;
    const recovery = v.action ? 0.14 : 0.025;
    v.temp += (setpoint - v.temp) * recovery + (v.action ? 0 : warming) + noise;
    const observed = v.temp + (v.fault === "sensor" ? run.minute * 0.045 : 0);
    const previous = v.readings.at(-1);
    const trend = previous ? observed - previous.temperature : 0;
    const breach = observed > v.high || observed < v.low;
    const risk = breach || (observed > setpoint + 1 && trend > 0.06);
    const prior = v.status;
    if (v.fault === "sensor") {
      v.status = "Sensor review";
      v.hold = 0;
    } else if (v.action) {
      v.hold = !breach && Math.abs(v.temp - old) < 0.12 ? v.hold + 1 : 0;
      v.status = v.hold >= 5 ? "Recovery verified" : "Verifying response";
    } else
      v.status = breach
        ? "Temperature breach"
        : risk
          ? "Review needed"
          : "Monitoring";
    v.readings.push({
      sequence: run.sequence,
      minute: run.minute,
      temperature: Number(observed.toFixed(3)),
      trend: Number(trend.toFixed(3)),
      status: v.status,
    });
    if (v.readings.length > 240) v.readings.shift();
    if (v.status !== prior)
      event(
        run,
        v,
        `${v.status}; observed ${observed.toFixed(2)} °C. Rule-based assessment.`,
      );
  }
  return run;
}
export function propose(run, id) {
  const v = run.vehicles.find((v) => v.id === id);
  if (!v) throw Error("Unknown vehicle.");
  if (v.fault === "sensor")
    throw Error(
      "Inspect the sensor: an intervention cannot establish sensor truth.",
    );
  if (!["Review needed", "Temperature breach"].includes(v.status))
    throw Error("No temperature response is currently required.");
  v.proposal = {
    sequence: run.sequence,
    fault: v.fault,
    action: "cold_storage",
    temperature: v.readings.at(-1).temperature,
  };
  event(run, v, "Cold-storage response proposed; waiting for human review.");
  return v.proposal;
}
export function approve(run, id) {
  const v = run.vehicles.find((v) => v.id === id),
    p = v?.proposal;
  if (!p) throw Error("Prepare a proposal first.");
  if (
    run.sequence - p.sequence > 5 ||
    p.fault !== v.fault ||
    !["Review needed", "Temperature breach"].includes(v.status)
  ) {
    v.proposal = null;
    throw Error(
      "Proposal expired or conditions changed. Prepare a fresh review.",
    );
  }
  v.proposal = null;
  v.action = "cold_storage";
  v.hold = 0;
  v.status = "Verifying response";
  event(
    run,
    v,
    "Human approved simulated cold storage. Verification starts on subsequent readings.",
  );
}
export function exportRun(run) {
  return {
    schema: 1,
    scope:
      "Browser-local live simulation; not fleet telemetry, backend approval, AI inference or benchmark evidence.",
    model:
      "First-order educational thermal model v1; rule-based decision policy",
    seed: run.seed,
    minute: run.minute,
    sample_limit: 240,
    event_limit: 80,
    vehicles: run.vehicles,
    events: run.events,
  };
}
