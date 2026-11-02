import {
  createRun,
  tick,
  injectFault,
  propose,
  approve,
  exportRun,
} from "./live-core.mjs?v=live-1";
import { engineeringDestination } from "./overview.mjs?v=axon-1";
const legacy = engineeringDestination(location.hash);
if (legacy) location.replace(new URL(legacy, location.href));
const $ = (id) => document.getElementById(id);
let activeView = "overview";
const acknowledged = new Set(),
  previousStatuses = new Map();

let run = createRun(),
  selected = run.vehicles[0].id,
  paused = false,
  timer = null;
const text = (tag, value) => {
  const n = document.createElement(tag);
  n.textContent = value;
  return n;
};
const svg = (tag, attrs, value) => {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
  if (value !== undefined) n.textContent = value;
  return n;
};
function notice(value) {
  $("live-notice").textContent = value;
}
function render() {
  renderPortal();
  const v = run.vehicles.find((v) => v.id === selected);
  $("run-clock").textContent = `Minute ${run.minute}`;
  $("run-status").textContent = paused
    ? "Paused · simulated"
    : document.hidden
      ? "Suspended · hidden tab"
      : "Streaming · simulated";
  $("pause-run").textContent = paused ? "Resume stream" : "Pause stream";
  $("review-count").textContent = run.vehicles.filter((v) =>
    ["Review needed", "Temperature breach", "Sensor review"].includes(v.status),
  ).length;
  $("recovery-count").textContent = run.vehicles.filter(
    (v) => v.status === "Recovery verified",
  ).length;
  $("sample-count").textContent = run.sequence * run.vehicles.length;
  const focusedId = document.activeElement?.dataset.vehicle;
  const list = run.vehicles.map((vehicle) => {
    const b = text("button", "");
    b.type = "button";
    b.dataset.vehicle = vehicle.id;
    b.setAttribute("aria-pressed", String(vehicle.id === selected));
    b.append(
      text("small", vehicle.id + " · " + vehicle.name),
      text(
        "strong",
        (vehicle.readings.at(-1)?.temperature ?? vehicle.temp).toFixed(2) +
          " °C",
      ),
      text("span", vehicle.status),
    );
    b.addEventListener("click", () => {
      selected = vehicle.id;
      render();
    });
    return b;
  });
  $("fleet-list").replaceChildren(...list);
  if (focusedId)
    list
      .find((b) => b.dataset.vehicle === focusedId)
      ?.focus({ preventScroll: true });
  $("vehicle-title").textContent = v.id + " / " + v.name;
  $("selected-status").textContent = v.status;
  $("fault-select").value = v.fault;
  const samples = v.readings.slice(-60),
    low = Math.min(v.low - 1, ...samples.map((p) => p.temperature)) - 1,
    high = Math.max(v.high + 1, ...samples.map((p) => p.temperature)) + 1;
  const y = (value) => 210 - ((value - low) / (high - low)) * 180;
  const chart = $("live-chart");
  chart.replaceChildren(
    svg(
      "title",
      { id: "live-chart-title" },
      "Generated temperature readings for " + v.id,
    ),
  );
  for (const limit of [v.low, v.high])
    chart.append(
      svg("line", {
        x1: 35,
        x2: 680,
        y1: y(limit),
        y2: y(limit),
        class: "live-chart-limit",
      }),
      svg(
        "text",
        { x: 40, y: y(limit) - 5, class: "live-chart-label" },
        limit + " °C limit",
      ),
    );
  if (samples.length)
    chart.append(
      svg("polyline", {
        points: samples
          .map(
            (p, i) =>
              `${35 + (i / Math.max(1, samples.length - 1)) * 645},${y(p.temperature)}`,
          )
          .join(" "),
        class: "live-chart-line",
      }),
    );
  const last = samples.at(-1);
  $("sample-details").textContent = last
    ? `Sample ${last.sequence} · simulated minute ${last.minute} · permitted ${v.low} to ${v.high} °C · ${v.hold}/5 stabilizing recovery samples`
    : "Awaiting first sample.";
  $("decision-explanation").textContent =
    v.fault === "sensor"
      ? "Sensor integrity is uncertain. Inspect the sensor; no automatic recovery claim is permitted."
      : v.action
        ? "A simulated cold-storage action is active. Fresh readings determine whether recovery can be verified."
        : ["Review needed", "Temperature breach"].includes(v.status)
          ? "Temperature is outside its limits or rising toward them. A simulated cold-storage response is available for human review."
          : "Readings are within the current monitoring rules. No temperature response is required.";
  $("prepare-response").disabled =
    !["Review needed", "Temperature breach"].includes(v.status) || !!v.action;
  $("approve-response").disabled = !v.proposal;
  $("proposal-details").textContent = v.proposal
    ? `Proposal at sample ${v.proposal.sequence}: cold storage. Expires after five additional samples. Approval affects this simulation only.`
    : "No pending proposal. Simulated approval never dispatches a real action.";
  const events = run.events.map((e) => {
    const li = text("li", "");
    li.append(
      text("small", `Minute ${e.minute} · ${e.vehicle} · sample ${e.sequence}`),
      text("span", e.text),
    );
    return li;
  });
  $("live-events").replaceChildren(
    ...(events.length
      ? events
      : [
          text(
            "li",
            "Monitoring started. Faults and decision changes will appear here.",
          ),
        ]),
  );
}
function schedule() {
  clearInterval(timer);
  timer = null;
  if (!paused && !document.hidden)
    timer = setInterval(
      () => {
        tick(run);
        render();
      },
      Number($("run-speed").value),
    );
  render();
}
$("pause-run").addEventListener("click", () => {
  paused = !paused;
  schedule();
});
$("run-speed").addEventListener("change", schedule);
$("fault-select").addEventListener("change", () => {
  injectFault(run, selected, $("fault-select").value);
  notice("Fault updated. Previous review invalidated.");
  render();
});
$("prepare-response").addEventListener("click", () => {
  try {
    propose(run, selected);
    notice("Response prepared. Review before approving.");
  } catch (e) {
    notice(e.message);
  }
  render();
});
$("approve-response").addEventListener("click", () => {
  try {
    approve(run, selected);
    notice(
      "Simulated response approved. Recovery is determined by subsequent readings.",
    );
  } catch (e) {
    notice(e.message);
  }
  render();
});
$("reset-run").addEventListener("click", () => {
  try {
    const next = createRun(Number($("run-seed").value));
    run = next;
    acknowledged.clear();
    previousStatuses.clear();
    selected = run.vehicles[0].id;
    loadNote();
    injectFault(run, selected, "cooling");
    tick(run);
    notice(
      "New run created. Previous run cleared; export before resetting to keep a copy.",
    );
    schedule();
  } catch (e) {
    notice(e.message);
  }
});
$("export-run").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(exportRun(run), null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `coldpath-simulated-run-${run.seed}-${run.sequence}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notice("Simulation run downloaded with seed, samples, events and scope.");
});
document.addEventListener("visibilitychange", schedule);
window.addEventListener("pagehide", () => clearInterval(timer));
window.addEventListener("pageshow", schedule);
injectFault(run, selected, "cooling");
tick(run);
schedule();

function needsReview(v) {
  return ["Review needed", "Temperature breach", "Sensor review"].includes(
    v.status,
  );
}
function changeView(view) {
  const names = {
    overview: "Overview",
    shipments: "Shipments",
    exceptions: "Exceptions",
    activity: "Activity log",
    handover: "Shift handover",
    settings: "Workspace settings",
  };
  if (!names[view]) view = "overview";
  activeView = view;
  if (view === "handover") loadNote();
  document
    .querySelectorAll("[data-portal-view]")
    .forEach(
      (node) =>
        (node.hidden = !node.dataset.portalView.split(" ").includes(view)),
    );
  document.querySelectorAll("[data-view]").forEach((node) => {
    if (node.dataset.view === view) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  $("view-title").textContent = names[view];
  document.querySelector("h1").textContent =
    view === "overview" ? "Operations overview" : names[view];
  document.querySelector(".live-grid").dataset.active = view;
  if (view === "exceptions") $("shipment-filter").value = "review";
  else if (view === "shipments") $("shipment-filter").value = "all";
  history.replaceState(null, "", "#" + view);
  renderPortal();
}
function renderPortal() {
  for (const v of run.vehicles) {
    if (previousStatuses.get(v.id) !== v.status) acknowledged.delete(v.id);
    previousStatuses.set(v.id, v.status);
  }
  $("exception-nav-count").textContent =
    run.vehicles.filter(needsReview).length;
  const query = $("shipment-search").value.trim().toLocaleLowerCase(),
    filter = $("shipment-filter").value;
  const rows = run.vehicles
    .filter((v) => (v.id + " " + v.name).toLocaleLowerCase().includes(query))
    .filter((v) =>
      activeView === "exceptions"
        ? needsReview(v)
        : filter === "review"
          ? needsReview(v)
          : filter === "monitoring"
            ? v.status === "Monitoring"
            : filter === "recovered"
              ? v.status === "Recovery verified"
              : true,
    );
  const focus = document.activeElement?.dataset?.record;
  $("shipment-rows").replaceChildren(
    ...rows.map((v) => {
      const tr = document.createElement("tr");
      if (v.id === selected) tr.className = "selected";
      const name = text("td", "");
      name.append(text("b", v.id), text("small", v.name));
      const temp = text(
          "td",
          (v.readings.at(-1)?.temperature ?? v.temp).toFixed(2) + " °C",
        ),
        range = text("td", `${v.low} to ${v.high} °C`),
        status = text("td", "");
      status.append(text("span", v.status));
      if (acknowledged.has(v.id))
        status.append(text("small", "Acknowledged · open"));
      const action = text("td", "");
      const inspect = text("button", "Inspect");
      inspect.dataset.record = v.id;
      inspect.addEventListener("click", () => {
        selected = v.id;
        render();
        $("vehicle-title").scrollIntoView({ block: "start" });
      });
      action.append(inspect);
      if (needsReview(v) && !acknowledged.has(v.id)) {
        const ack = text("button", "Acknowledge");
        ack.addEventListener("click", () => {
          acknowledged.add(v.id);
          run.events.unshift({
            sequence: run.sequence,
            minute: run.minute,
            vehicle: v.id,
            text: "Exception acknowledged locally; condition remains open.",
          });
          run.events.length = Math.min(80, run.events.length);
          notice(
            "Exception acknowledged. Monitoring and review remain active.",
          );
          render();
        });
        action.append(ack);
      }
      tr.append(name, temp, range, status, action);
      return tr;
    }),
  );
  if (focus)
    $("shipment-rows")
      .querySelector(`[data-record="${focus}"]`)
      ?.focus({ preventScroll: true });
  $("register-count").textContent =
    `${rows.length} of ${run.vehicles.length} shipments`;
  $("register-empty").hidden = rows.length > 0;
}
function noteKey() {
  return `axon-handover-v1:${run.seed}:${$("handover-shipment").value}`;
}
function loadNote() {
  try {
    $("handover-note").value = localStorage.getItem(noteKey()) || "";
    $("handover-status").textContent = "Stored on this browser only.";
  } catch {
    $("handover-note").value = "";
    $("handover-status").textContent =
      "Browser storage unavailable. Download notes to keep them.";
  }
}
function saveFile(contents, name, type) {
  const url = URL.createObjectURL(new Blob([contents], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () => changeView(button.dataset.view)),
  );
$("open-settings").addEventListener("click", () => changeView("settings"));
$("shipment-search").addEventListener("input", renderPortal);
$("shipment-filter").addEventListener("change", renderPortal);
$("clear-filters").addEventListener("click", () => {
  $("shipment-search").value = "";
  $("shipment-filter").value = activeView === "exceptions" ? "review" : "all";
  renderPortal();
});
$("handover-shipment").addEventListener("change", loadNote);
$("save-handover").addEventListener("click", () => {
  try {
    localStorage.setItem(noteKey(), $("handover-note").value);
    $("handover-status").textContent = "Handover saved on this browser.";
  } catch {
    $("handover-status").textContent =
      "Storage unavailable. Use Download handover.";
  }
});
$("download-handover").addEventListener("click", () =>
  saveFile(
    `Axon Services — shift handover\nShipment: ${$("handover-shipment").value}\nTraining run seed: ${run.seed}\nSimulated minute: ${run.minute}\n\n${$("handover-note").value}\n\nTraining environment; browser-local note, no team delivery.`,
    "axon-services-handover.txt",
    "text/plain",
  ),
);
changeView(location.hash.slice(1));
loadNote();
