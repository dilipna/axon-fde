import { engineeringDestination } from "../../apps/control_tower/overview.mjs";
import {
  getStory,
  readStudyLocation,
  studyHash,
} from "../../apps/control_tower/story.mjs";
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import {
  verifyBundle,
  sha256,
  rankPlanning,
  answerRecorded,
  planningKey,
  makePacket,
  makeBrief,
} from "../../apps/control_tower/workbench-core.mjs";
const root = new URL("../../", import.meta.url);
const text = await fs.readFile(
  new URL("apps/control_tower/workbench-data.json", root),
  "utf8",
);
const manifest = JSON.parse(
  await fs.readFile(
    new URL("apps/control_tower/workbench-manifest.json", root),
    "utf8",
  ),
);
const data = await verifyBundle(manifest, text);
const parity = JSON.parse(
  await fs.readFile(
    new URL("apps/control_tower/workbench-parity.json", root),
    "utf8",
  ),
);
for (const [i, fixture] of parity.entries())
  test(`browser planning matches Python backend case ${i}`, () => {
    const actual = rankPlanning(
      data.planning_policy,
      fixture.probability,
      fixture.value,
      fixture.blocked,
    );
    assert.deepEqual(
      actual.map((x) => x.id),
      fixture.options.map((x) => x.id),
    );
    actual.forEach((x, index) => {
      assert.equal(x.feasible, fixture.options[index].feasible);
      assert.ok(Math.abs(x.ev - fixture.options[index].ev) < 1e-7);
    });
  });
test("fingerprint detects changed or partial evidence", async () => {
  await assert.rejects(() => verifyBundle(manifest, text + " "), /fingerprint/);
  await assert.rejects(
    () => verifyBundle({ ...manifest, payload: "https://evil.test" }, text),
    /manifest/,
  );
});
for (const value of [NaN, Infinity, -1])
  test(`non-finite or negative value ${value} rejects`, () =>
    assert.throws(() => rankPlanning(data.planning_policy, 0.5, value)));
test("do nothing remains available, and unknown blocked actions reject", () => {
  assert.equal(
    rankPlanning(data.planning_policy, 0.5, 100, ["do_nothing"]).find(
      (x) => x.id === "do_nothing",
    ).feasible,
    true,
  );
  assert.throws(() =>
    rankPlanning(data.planning_policy, 0.5, 100, ["invented"]),
  );
});
test("recorded assistant cites the stale approval and retains the failed outcome", () => {
  const approval = answerRecorded("why was approval blocked?", data);
  assert.ok(approval.text.includes("approval_stale"));
  assert.ok(approval.citations.includes("trace.steps[11]"));
  assert.ok(
    answerRecorded("what did verification find?", data).text.includes(
      "NOT rerouted",
    ),
  );
  assert.equal(
    answerRecorded("what is the real customer revenue?", data).citations.length,
    0,
  );
  assert.ok(
    answerRecorded("who approved it?", data, "sensor").text.includes(
      "not a recorded approval",
    ),
  );
});
test("changed assumptions invalidate rehearsal review and packets preserve scope", () => {
  const planning = { probability: 0.5, value: 184000, blocked: [] };
  const key = planningKey(planning, manifest.payload_sha256);
  const packet = makePacket({
    data,
    manifest,
    caseId: "compressor",
    notes: "My note",
    planning: { ...planning, probability: 0.6 },
    reviewKey: key,
  });
  assert.equal(
    packet.planning.review_status,
    "not reviewed or assumptions changed",
  );
  assert.equal(packet.planning.calibrated_probability, false);
  assert.ok(packet.scope.includes("not live"));
  assert.equal(
    makePacket({
      data,
      manifest,
      caseId: "sensor",
      notes: "",
      planning,
      reviewKey: null,
    }).recorded_steps.length,
    0,
  );
});

test("rehearsal review cannot carry across studies", () => {
  const p = { probability: 0.5, value: 100, blocked: [] };
  assert.notEqual(
    planningKey(p, manifest.payload_sha256, "compressor"),
    planningKey(p, manifest.payload_sha256, "sensor"),
  );
  const key = planningKey(p, manifest.payload_sha256, "compressor");
  assert.equal(
    makePacket({
      data,
      manifest,
      caseId: "sensor",
      planning: p,
      notes: "",
      reviewKey: key,
    }).planning.review_status,
    "not reviewed or assumptions changed",
  );
});
test("shift brief preserves scope and renders untrusted HTML as literal text", () => {
  const p = { probability: 0.5, value: 100, blocked: [] };
  const packet = makePacket({
    data,
    manifest,
    caseId: "sensor",
    planning: p,
    notes: "<script>alert(1)</script>",
    reviewKey: null,
  });
  const brief = makeBrief(packet);
  assert.ok(brief.includes("&lt;script&gt;"));
  assert.ok(!brief.includes("<script>"));
  assert.ok(brief.includes("not live"));
});

test("guided story derives warning times from evidence and preserves the failed result", () => {
  const story = getStory(data, "compressor");
  assert.equal(story.length, 4);
  assert.ok(story[0].copy.includes(String(data.trace.facts.lead_time_minutes)));
  assert.ok(story[3].copy.includes("failed"));
  assert.ok(story[3].copy.includes("cannot prove"));
  for (const chapter of story)
    for (const source of chapter.sources)
      assert.ok(data.trace.steps.some((step) => step.number === source));
  const changed = structuredClone(data);
  changed.trace.facts.lead_time_minutes = 17;
  assert.ok(
    getStory(changed, "compressor")[0].callout.startsWith("17 minutes"),
  );
  assert.throws(() => getStory(data, "unknown"));
});
test("sensor story does not invent approvals or a recorded intervention", () => {
  const story = getStory(data, "sensor");
  assert.equal(story.length, 4);
  assert.ok(story.every((chapter) => chapter.sources.length === 0));
  assert.ok(story[3].title.includes("no recorded intervention"));
});
test("shared story links round-trip case and chapter without private inputs", () => {
  for (const caseId of ["compressor", "sensor"])
    for (let chapter = 0; chapter < 4; chapter++)
      assert.deepEqual(readStudyLocation(studyHash(caseId, chapter)), {
        caseId,
        chapter,
      });
  assert.deepEqual(readStudyLocation("#case=evil&chapter=999"), {
    caseId: "compressor",
    chapter: 0,
  });
  assert.deepEqual(readStudyLocation("#case=sensor&chapter=-1"), {
    caseId: "sensor",
    chapter: 0,
  });
  assert.throws(() => studyHash("compressor", NaN));
  assert.throws(() => studyHash("invalid", 0));
});
test("readable exports retain the plain-language outcome and unavailable assumptions", () => {
  const planning = { probability: 0.5, value: 100, blocked: ["trailer_swap"] };
  const packet = makePacket({
    data,
    manifest,
    caseId: "compressor",
    planning,
    notes: "A note",
    reviewKey: null,
  });
  assert.equal(packet.product_name, "Coldpath");
  assert.equal(packet.guided_summary.length, 4);
  const brief = makeBrief(packet);
  assert.ok(brief.startsWith("# Coldpath"));
  assert.ok(brief.includes("cannot prove"));
  assert.ok(brief.includes("Unavailable responses: trailer_swap"));
});

test("published engineering bookmarks retain their destination without open redirects", () => {
  assert.equal(
    engineeringDestination("#engineering"),
    "engineering.html#engineering",
  );
  assert.equal(engineeringDestination("#replay"), "engineering.html#replay");
  for (const hash of [
    "#how",
    "#case=sensor",
    "#https://evil.test",
    "#replay?next=https://evil.test",
  ])
    assert.equal(engineeringDestination(hash), null);
});

for (const [label, mutate] of [
  ["missing warning facts", (copy) => delete copy.trace.facts],
  [
    "inconsistent warning interval",
    (copy) => (copy.trace.facts.lead_time_minutes += 1),
  ],
  [
    "missing cited story step",
    (copy) =>
      (copy.trace.steps = copy.trace.steps.filter(
        (step) => step.number !== 13,
      )),
  ],
])
  test(`story refuses ${label} even when supplied with a matching manifest`, async () => {
    const copy = structuredClone(data);
    mutate(copy);
    const body = JSON.stringify(copy);
    const payloadHash = await sha256(body);
    await assert.rejects(
      () => verifyBundle({ ...manifest, payload_sha256: payloadHash }, body),
      /warning facts|story source/,
    );
  });
