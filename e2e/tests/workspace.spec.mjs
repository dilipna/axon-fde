import { test, expect } from "@playwright/test";
import fs from "node:fs/promises";
import { createHash } from "node:crypto";

const data = JSON.parse(
  await fs.readFile(
    new URL("../../apps/control_tower/workbench-data.json", import.meta.url),
    "utf8",
  ),
);
const manifest = JSON.parse(
  await fs.readFile(
    new URL(
      "../../apps/control_tower/workbench-manifest.json",
      import.meta.url,
    ),
    "utf8",
  ),
);
test.beforeAll(async ({ request }) => {
  const response = await request.get("ui/workbench-release.json");
  expect(response.ok()).toBe(true);
  const identity = await response.json();
  expect(identity.dirty_source).toBe(false);
  expect(identity.source_tree).toBe(process.env.AXON_EXPECTED_TREE);
});
test.beforeEach(async ({ page, baseURL }) => {
  await page.route("**/*", (route) =>
    new URL(route.request().url()).origin === new URL(baseURL).origin
      ? route.continue()
      : route.abort(),
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
});
async function open(page) {
  await page.goto("workbench.html");
  await expect(page.locator("#app-shell")).toBeVisible();
}
for (const width of [1440, 768, 390, 360])
  test(`recorded workspace journey at ${width}px`, async ({ page }, info) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("./");
    await page
      .getByRole("link", { name: "Explore a delivery", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "An alert is just the beginning." }),
    ).toBeVisible();
    await expect(page.locator("#fact-value")).toHaveText("35 min");
    await expect(page.locator("#fact-label")).toContainText(
      "Not a fleet average",
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await expect(
      page.getByRole("button", { name: "Play recorded timeline", exact: true }),
    ).toBeDisabled();
    await page.locator("#timeline").focus();
    await page.keyboard.press("End");
    await expect(page.locator("#minute-value")).toHaveText("239");
    await page
      .getByRole("button", { name: "Why was it blocked?", exact: true })
      .click();
    await expect(page.locator("#answer")).toContainText("approval_stale");
    await page.getByRole("link", { name: "Step 11 â†—", exact: true }).click();
    await expect(page.locator("#record-step-11")).toHaveAttribute("open", "");
    await page
      .locator("#notes")
      .fill("Check facility capacity before approving.");
    await page.reload();
    await expect(page.locator("#notes")).toHaveValue(
      "Check facility capacity before approving.",
    );
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page
        .getByRole("button", {
          name: "Download evidence packet â†“",
          exact: true,
        })
        .click(),
    ]);
    const packet = JSON.parse(await fs.readFile(await download.path(), "utf8"));
    expect(packet.scope).toContain("not live");
    expect(packet.notes).toContain("facility capacity");
    expect(packet.source_fingerprint).toBe(manifest.payload_sha256);
    expect(packet.planning.calibrated_probability).toBe(false);
    expect(packet.recorded_steps).toHaveLength(14);
    await download.saveAs(info.outputPath(`decision-${width}.json`));
    await page.getByRole("button", { name: /Can we trust the sensor/ }).click();
    await expect(page.locator("#notes")).toHaveValue("");
    await expect(page.locator("#record-steps")).toContainText(
      "No decision history is invented",
    );
    await page
      .getByRole("button", { name: "Did it work?", exact: true })
      .click();
    await expect(page.locator("#answer")).toContainText(
      "not a recorded approval",
    );
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.getByRole("button", { name: /Medicine delivery/ }).click();
    await expect(page.locator("#notes")).toHaveValue(
      "Check facility capacity before approving.",
    );
    await page.screenshot({
      path: info.outputPath(`workspace-${width}.png`),
      fullPage: true,
    });
    expect(errors).toEqual([]);
  });
test("recorded playback responds to play, pause and scrubbing", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await open(page);
  const before = await page.locator("#minute-value").textContent();
  await page
    .getByRole("button", { name: "Play recorded timeline", exact: true })
    .click();
  await expect
    .poll(() => page.locator("#minute-value").textContent())
    .not.toBe(before);
  await page
    .getByRole("button", { name: "Pause recorded timeline", exact: true })
    .click();
  const stopped = await page.locator("#minute-value").textContent();
  await page.waitForTimeout(600);
  expect(await page.locator("#minute-value").textContent()).toBe(stopped);
  await page.locator("#timeline").focus();
  await page.keyboard.press("Home");
  await expect(page.locator("#minute-value")).toHaveText("0");
});
test("record search, source citations and unknown questions retain evidence boundaries", async ({
  page,
}) => {
  await open(page);
  await page.locator("#technical-details > summary").click();
  await page.locator("#record-search").fill("approval_stale");
  await expect(page.locator("#record-steps details")).toHaveCount(1);
  await page.getByRole("button", { name: "Did it work?", exact: true }).click();
  await expect(page.locator("#answer")).toContainText("NOT rerouted");
  await expect(page.locator("#answer")).toContainText("incident reopened");
  await page.locator("#question").fill("What was the real customer revenue?");
  await page.getByRole("button", { name: "Ask â†—", exact: true }).click();
  await expect(page.locator("#answer")).toContainText("does not establish");
});
test("planning changes invalidate rehearsal review and exclude unavailable options", async ({
  page,
}) => {
  await open(page);
  await page.locator("#planning-drawer > summary").click();
  await page
    .getByRole("button", {
      name: "Mark these assumptions reviewed",
      exact: true,
    })
    .click();
  await expect(page.locator("#review-status")).toContainText(
    "No backend approval",
  );
  await page.locator("#facility-unavailable").check();
  await expect(page.locator("#review-status")).toContainText(
    "Assumptions changed",
  );
  await expect(
    page
      .locator("#planning-options li")
      .filter({ hasText: "Reroute to cold storage" }),
  ).toContainText("Excluded");
  await page.locator("#planning-value").fill("-1");
  await expect(page.locator("#planning-error")).toContainText("finite risk");
  await expect(page.locator("#export")).toBeDisabled();
  await page.locator("#planning-value").fill("184000");
  await expect(page.locator("#export")).toBeEnabled();
  await page
    .getByRole("button", {
      name: "Mark these assumptions reviewed",
      exact: true,
    })
    .click();
  await page.locator("[data-case=sensor]").click();
  await expect(page.locator("#review-status")).toContainText(
    "Assumptions changed",
  );
});
test("frozen verifier cases show false confirmations alongside abstention costs", async ({
  page,
}) => {
  await open(page);
  await page.locator("#technical-details > summary").click();
  await expect(page.locator("#fault-case option")).toHaveCount(24);
  await page
    .locator("#fault-case")
    .selectOption("temperature_within_envelope/stuck_hiding_failure");
  await expect(page.locator(".fault-box").first()).toContainText("confirmed");
  await expect(page.locator(".fault-box").last()).toContainText("inconclusive");
  await expect(page.locator("#fault-summary")).toContainText(
    "false confirmations 6 â†’ 2",
  );
  await expect(page.locator("#fault-summary")).toContainText(
    "missed recoveries 0 â†’ 4",
  );
  await expect(page.locator("#fault-summary")).toContainText(
    "abstentions 4 â†’ 12",
  );
});
test("shift briefing exports user notes as safe literal text with original scope", async ({
  page,
}, info) => {
  await open(page);
  await page.locator("#notes").fill("<script>alert(1)</script>");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page
      .getByRole("button", {
        name: "Download my readable brief â†“",
        exact: true,
      })
      .click(),
  ]);
  const text = await fs.readFile(await download.path(), "utf8");
  expect(text).toContain("&lt;script&gt;");
  expect(text).not.toContain("<script>");
  expect(text).toContain("not live fleet");
  await download.saveAs(info.outputPath("shift-brief.md"));
});
test("malformed evidence fails closed rather than inventing a replacement view", async ({
  page,
}) => {
  await page.route("**/ui/workbench-data.json", async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, body: (await response.text()) + " " });
  });
  await page.goto("workbench.html");
  await expect(page.getByRole("alert")).toContainText("fingerprint mismatch");
  await expect(page.locator("#app-shell")).toBeHidden();
});
test("untrusted source text stays literal under strict content policy", async ({
  page,
}) => {
  const injected = structuredClone(data);
  injected.trace.steps.find((s) => s.number === 11).lines[1].text =
    '<img src=x onerror="window.__executed=true">';
  const text = JSON.stringify(injected);
  const receipt = {
    ...manifest,
    payload_sha256: createHash("sha256").update(text).digest("hex"),
  };
  // Fulfil the exact bytes whose hash was computed.
  await page.route("**/ui/workbench-data.json", (route) =>
    route.fulfill({ body: text, contentType: "application/json" }),
  );
  await page.route("**/ui/workbench-manifest.json", (route) =>
    route.fulfill({ json: receipt }),
  );
  await page.addInitScript(() => {
    window.__executed = false;
  });
  await open(page);
  await page.locator("#technical-details > summary").click();
  await page.locator("#record-search").fill("onerror");
  await page.locator("#record-step-11 summary").click();
  await expect(page.locator("#record-step-11")).toContainText("<img src=x");
  expect(await page.evaluate(() => window.__executed)).toBe(false);
  await expect(page.locator("#record-step-11 img")).toHaveCount(0);
});
test("workspace information and model record are keyboard accessible", async ({
  page,
}) => {
  await open(page);
  await page
    .getByRole("button", { name: "About Coldpath", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText(
    "not a backend approval",
  );
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await page.locator("#technical-details > summary").click();
  await page.locator(".recorded-model summary").focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("#model-identity")).toContainText(
    "openai/gpt-oss-120b",
  );
  await expect(page.locator("#model-narrative")).toBeVisible();
  await page.locator("h1").click();
  await page.keyboard.press("/");
  await expect(page.locator("#record-search")).toBeFocused();
});
test("browser storage failure does not lose the in-memory export", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => {
      throw new Error("Storage unavailable for test");
    };
  });
  await open(page);
  await page.locator("#notes").fill("Keep this note in the exported packet.");
  await expect(page.locator("#notes-status")).toContainText("Could not save");
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.locator("#export").click(),
  ]);
  const packet = JSON.parse(await fs.readFile(await download.path(), "utf8"));
  expect(packet.notes).toBe("Keep this note in the exported packet.");
});
test("sharing and study links never include private notes or planning inputs", async ({
  page,
  context,
  baseURL,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"], {
    origin: new URL(baseURL).origin,
  });
  await open(page);
  await page.locator("#notes").fill("private note");
  await page.locator("[data-case=sensor]").click();
  await page
    .getByRole("button", { name: "Copy study link â†—", exact: true })
    .click();
  const link = await page.evaluate(() => navigator.clipboard.readText());
  expect(link).toContain("workbench.html#case=sensor");
  expect(link).not.toContain("private");
  expect(link).not.toContain("planning");
});
test("reading and rehearsal never issue write requests or external model calls", async ({
  page,
  baseURL,
}) => {
  const requests = [];
  page.on("request", (request) =>
    requests.push({
      method: request.method(),
      origin: new URL(request.url()).origin,
    }),
  );
  await open(page);
  await page.locator("#planning-drawer > summary").click();
  await page.locator("#planning-value").fill("200000");
  await page.locator("#practice-review").click();
  await page
    .getByRole("button", { name: "Why this option?", exact: true })
    .click();
  expect(
    requests.every(
      (r) => r.method === "GET" && r.origin === new URL(baseURL).origin,
    ),
  ).toBe(true);
});

for (const width of [1440, 768, 390])
  test(`original project replay remains functional at ${width}px`, async ({
    page,
    request,
  }, info) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("engineering.html");
    await expect(page.locator("#timeline .tstep").first()).toBeVisible();
    await expect(page.locator("#claim-grid .claim").first()).toBeVisible();
    await page.locator("#r-play").click();
    await expect(page.locator("#r-play")).toContainText("Pause");
    await page.locator("#r-restart").click();
    await expect(page.locator("#r-scrub")).toHaveValue("0");
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    for (const path of [
      "ui/verification-release.json",
      "api/v1/control/trace",
    ]) {
      const response = await request.get(path);
      expect(response.ok()).toBe(true);
      const saved = JSON.parse(
        await fs.readFile(
          new URL("../../site/" + path, import.meta.url),
          "utf8",
        ),
      );
      expect(await response.json()).toEqual(saved);
    }
    await page.locator("#engineering").scrollIntoViewIfNeeded();
    await page.screenshot({
      path: info.outputPath(`original-engineering-${width}.png`),
    });
    expect(errors).toEqual([]);
  });

for (const width of [1440, 768, 390, 360])
  test(`Coldpath overview is understandable and usable at ${width}px`, async ({
    page,
  }, info) => {
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("./");
    await expect(page).toHaveTitle(/Coldpath/);
    await expect(page.getByRole("heading", { level: 1 })).toContainText(
      "A delivery at risk.",
    );
    await expect(
      page.getByText(
        "Interactive engineering demo Â· fictional shipments Â· recorded results",
      ),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Explore a delivery", exact: false }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: info.outputPath(`coldpath-overview-${width}.png`),
      fullPage: true,
    });
    await page
      .getByRole("link", { name: "See the honest outcome", exact: false })
      .click();
    await expect(page.locator("#story-title")).toContainText(
      "incident reopened",
    );
    expect(errors).toEqual([]);
  });
test("guided story advances, reveals its source, wraps and preserves the failed outcome", async ({
  page,
}) => {
  await open(page);
  await expect(page.locator("#story-previous")).toBeDisabled();
  await expect(page.locator("#technical-details")).not.toHaveAttribute(
    "open",
    "",
  );
  await expect(page.locator("#story-copy")).toContainText("35 minutes");
  await page.locator("#story-next").click();
  await expect(page.locator("#story-title")).toContainText("temperature limit");
  await page.locator("#story-evidence").click();
  await expect(page.locator("#technical-details")).toHaveAttribute("open", "");
  await expect(page.locator("#record-step-3")).toHaveAttribute("open", "");
  await page.locator("#return-story").click();
  await expect(page.locator("#story-title")).toBeFocused();
  await page.locator("#story-next").click();
  await expect(page.locator("#story-copy")).toContainText("outdated approval");
  await page.locator("#story-next").click();
  await expect(page.locator("#story-copy")).toContainText("cannot prove");
  await expect(page.locator("#story-title")).toContainText("incident reopened");
  await page.reload();
  await expect(page.locator("#story-counter")).toHaveText("4 of 4");
  await page.locator("#story-next").click();
  await expect(page.locator("#story-counter")).toHaveText("1 of 4");
});
test("beginner view keeps advanced controls optional and keeps existing notes", async ({
  page,
}) => {
  await page.addInitScript(
    ({ fingerprint }) =>
      localStorage.setItem(
        `axon-workbench-notes-v1:${fingerprint}:compressor`,
        "A note from before the rename.",
      ),
    { fingerprint: manifest.payload_sha256 },
  );
  await open(page);
  await expect(page.locator("#notes")).toHaveValue(
    "A note from before the rename.",
  );
  await expect(page.locator("#record-search")).toBeHidden();
  await expect(page.locator("#planning-value")).toBeHidden();
  await expect(page.locator("#story-title")).toBeVisible();
  await page.locator("#story-evidence").click();
  await expect(page.locator("#record-step-5")).toHaveAttribute("open", "");
  await page.locator("[data-case=sensor]").click();
  await expect(page.locator("#notes")).toHaveValue("");
  await page.locator('[data-chapter="3"]').click();
  await expect(page.locator("#story-title")).toContainText(
    "no recorded intervention",
  );
});
test("chapter links preserve the chosen study without claiming a sensor recovery", async ({
  page,
}) => {
  await page.goto("workbench.html#case=sensor&chapter=3");
  await expect(page.locator("#app-shell")).toBeVisible();
  await expect(page.locator("#story-counter")).toHaveText("4 of 4");
  await expect(page.locator("#story-title")).toContainText(
    "no recorded intervention",
  );
  await expect(page.locator("#move-title")).toContainText(
    "Check before calling it safe",
  );
  await page.locator("#story-evidence").click();
  await expect(page.locator("#fault-case")).toBeFocused();
});

test("existing evidence bookmarks still reach the preserved engineering page", async ({
  page,
}) => {
  await page.goto("./#engineering");
  await expect(page).toHaveURL(/engineering\.html#engineering$/);
  await expect(page.locator("#engineering")).toBeVisible();
  await expect(page.locator(".logo")).toContainText("Coldpath");
});
