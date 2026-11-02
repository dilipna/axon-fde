import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createRun,
  tick,
  injectFault,
  propose,
  approve,
  exportRun,
} from "../../apps/control_tower/live-core.mjs";
const advance = (r, n) => {
  for (let i = 0; i < n; i++) tick(r);
  return r;
};
test("same seed and controls reproduce a run", () => {
  const a = createRun(17),
    b = createRun(17);
  for (const r of [a, b]) {
    injectFault(r, "SIM-101", "cooling");
    advance(r, 30);
  }
  assert.deepEqual(a, b);
});
test("healthy baseline stays within its envelope", () => {
  const r = advance(createRun(), 300);
  for (const v of r.vehicles) {
    assert.equal(v.status, "Monitoring");
    assert.ok(
      v.readings.every(
        (p) => p.temperature >= v.low && p.temperature <= v.high,
      ),
    );
  }
});
test("a cooling fault produces review then breach without intervention", () => {
  const r = createRun();
  injectFault(r, "SIM-101", "cooling");
  advance(r, 10);
  assert.equal(r.vehicles[0].status, "Review needed");
  advance(r, 30);
  assert.equal(r.vehicles[0].status, "Temperature breach");
});
test("approval changes future samples and requires sustained verification", () => {
  const r = createRun();
  injectFault(r, "SIM-101", "cooling");
  advance(r, 25);
  propose(r, "SIM-101");
  approve(r, "SIM-101");
  assert.equal(r.vehicles[0].status, "Verifying response");
  tick(r);
  assert.notEqual(r.vehicles[0].status, "Recovery verified");
  advance(r, 40);
  assert.equal(r.vehicles[0].status, "Recovery verified");
  assert.ok(r.events.some((e) => e.text.includes("Human approved")));
});
test("expired proposals cannot execute", () => {
  const r = createRun();
  injectFault(r, "SIM-101", "cooling");
  advance(r, 20);
  propose(r, "SIM-101");
  advance(r, 6);
  assert.throws(() => approve(r, "SIM-101"), /expired/);
  assert.equal(r.vehicles[0].action, null);
});
test("a fault change invalidates review", () => {
  const r = createRun();
  injectFault(r, "SIM-101", "cooling");
  advance(r, 20);
  propose(r, "SIM-101");
  injectFault(r, "SIM-101", "door");
  assert.throws(() => approve(r, "SIM-101"), /proposal first/);
});
test("sensor drift cannot claim intervention recovery", () => {
  const r = createRun();
  injectFault(r, "SIM-101", "sensor");
  advance(r, 100);
  assert.equal(r.vehicles[0].status, "Sensor review");
  assert.throws(() => propose(r, "SIM-101"), /sensor/);
  assert.equal(r.vehicles[0].hold, 0);
});
test("repeated approval cannot dispatch a second action", () => {
  const r = createRun();
  injectFault(r, "SIM-101", "cooling");
  advance(r, 20);
  propose(r, "SIM-101");
  approve(r, "SIM-101");
  assert.throws(() => approve(r, "SIM-101"), /proposal first/);
});
test("buffers and exports retain truthful scope", () => {
  const r = createRun();
  for (let i = 0; i < 350; i++) {
    injectFault(r, "SIM-101", i % 2 ? "door" : "cooling");
    tick(r);
  }
  assert.ok(r.events.length <= 80);
  assert.equal(r.vehicles[0].readings.length, 240);
  assert.match(exportRun(r).scope, /not fleet telemetry/);
  assert.equal(JSON.parse(JSON.stringify(exportRun(r))).seed, 42);
});
test("invalid seeds and fault inputs reject", () => {
  for (const seed of [0, NaN, 1.1, 1000000])
    assert.throws(() => createRun(seed));
  assert.throws(() => injectFault(createRun(), "unknown", "cooling"));
  assert.throws(() => injectFault(createRun(), "SIM-101", "unknown"));
});
