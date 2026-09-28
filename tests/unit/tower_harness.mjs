// Loads the control tower's app.js *without* its boot sequence and exercises
// its pure functions. Driven by tests/unit/test_control_tower_js.py.
//
// app.js is one module that boots itself against a DOM. Everything above the
// "Boot" banner is declarations only, so the harness takes that prefix, adds
// an export line, and imports it - the functions under test are the shipped
// ones, byte for byte, not copies that could drift.
import { readFileSync } from "node:fs";

const [appPath, inputPath] = process.argv.slice(2);
const source = readFileSync(appPath, "utf8");
const cut = source.indexOf("/* ── Boot");
if (cut < 0) throw new Error("app.js has no Boot banner; the harness cannot isolate its functions");
const module = await import(
  "data:text/javascript;base64," +
    Buffer.from(
      source.slice(0, cut) +
        "\nexport { slope, c1Pair, CLAIM_CARDS, SLOPE_FROM, SLOPE_TO, stripAnsi };\n"
    ).toString("base64")
);

const input = JSON.parse(readFileSync(inputPath, "utf8"));
const out = { slopes: {}, c1: {} };
for (const [scenario, points] of Object.entries(input.telemetry)) {
  out.slopes[scenario] = {
    cargo_temp_c: module.slope(points, "cargo_temp_c"),
    compressor_rpm: module.slope(points, "compressor_rpm"),
  };
}
out.window = [module.SLOPE_FROM, module.SLOPE_TO];
out.c1.with_far = module.c1Pair({ companions: { false_alarm_rate: 0.3, baseline_false_alarm_rate: 0.2 } });
out.c1.without_far = module.c1Pair({ companions: {} });
out.c1.required = module.CLAIM_CARDS.find((c) => c.id === "C1")?.required === true;
out.ansi = module.stripAnsi("CS-13: \u001b[33mfull\u001b[0m");
console.log(JSON.stringify(out));
