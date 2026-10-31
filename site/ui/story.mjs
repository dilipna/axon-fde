/* Plain-language editorial guide to the unchanged recording. Not a model response. */
export function getStory(data, caseId) {
  if (caseId === "sensor")
    return [
      {
        label: "The reading",
        kicker: "A NUMBER IS A CLUE",
        title: "A temperature reading is not the whole picture.",
        copy: "This second study contains a saved sensor-drift recording. It lets you explore what the sensor reported, without pretending we know the physical truth from that one channel.",
        callout:
          "Start with the observed reading. Keep its uncertainty in view.",
        boundary:
          "This is a telemetry comparison, not a recorded delivery intervention.",
        sources: [],
        minute: 0,
      },
      {
        label: "The doubt",
        kicker: "WHAT IF THE SENSOR IS WRONG?",
        title: "A sensible-looking value can still mislead.",
        copy: "A sensor can drift or become stuck. A steady number could mean stable cargo—or a faulty sensor. The available evidence does not settle that on its own.",
        callout: "“It looks normal” is not the same as “it is trustworthy.”",
        boundary: "No field sensor-trust claim is made.",
        sources: [],
        minute: 80,
      },
      {
        label: "The check",
        kicker: "KNOW WHEN TO ASK FOR MORE",
        title: "Sometimes the useful answer is “not enough evidence.”",
        copy: "The frozen test cases show the tradeoff: being more cautious reduces some false confirmations, but also produces more uncertain results and misses some recoveries.",
        callout:
          "Open the test cases below to see the benefits and the cost of caution.",
        boundary:
          "Constructed tests do not establish accuracy in a real fleet.",
        sources: [],
        minute: 160,
      },
      {
        label: "The limit",
        kicker: "NO HAPPY ENDING INVENTED",
        title: "There is no recorded intervention for this study.",
        copy: "We can inspect the readings. We cannot claim that a manager approved a response or that a real shipment recovered. Coldpath keeps that missing evidence visible.",
        callout: "Do not turn a missing result into a success story.",
        boundary:
          "Use the medicine-delivery study for the recorded approval and execution journey.",
        sources: [],
        minute: 239,
      },
    ];
  if (caseId !== "compressor") throw Error("Unknown story");
  const lead = data.trace.facts.lead_time_minutes;
  const warning = data.trace.facts.detected_at_minute;
  const alarm = data.trace.facts.baseline_alarm_minute;
  return [
    {
      label: "The warning",
      kicker: "01 / NOTICE THE RISK",
      title:
        "The medicines were warming up. The ordinary alarm was still quiet.",
      copy: `In this recording, the system noticed a warning at minute ${warning}. The temperature-limit alarm did not fire until minute ${alarm}. That gave this one incident ${lead} minutes of earlier warning—not a promise for every delivery.`,
      callout: `${lead} minutes earlier in this recorded case. A chance to investigate, not proof of avoided loss.`,
      boundary:
        "These are synthetic recorded observations, not live vehicle data.",
      sources: [5],
      minute: warning,
    },
    {
      label: "The rules",
      kicker: "02 / GET THE RIGHT CONTEXT",
      title: "Which temperature limit actually applies?",
      copy: "The delivery record and the shipment document disagreed. The recorded investigation used the Bill of Lading’s 2–8 °C range. Using the looser 10 °C database limit would have hidden the risk.",
      callout:
        "A correct reading can still lead to a wrong decision if the rule is wrong.",
      boundary:
        "The document-resolution decision is preserved in the original record.",
      sources: [3, 4],
      minute: warning,
    },
    {
      label: "The decision",
      kicker: "03 / REVIEW BEFORE ACTING",
      title:
        "Cold storage was the proposed move. Approval still had to stay current.",
      copy: "The recorded cost comparison favored rerouting to cold storage. Then new readings arrived after the first approval. The system blocked that outdated approval, required a fresh one, and recorded a simulated reroute without dispatching a second action on retry.",
      callout:
        "New evidence means a fresh review. A model’s suggestion is never permission to act.",
      boundary:
        "This was a local simulated carrier action. The public website cannot approve or dispatch it.",
      sources: [8, 11, 12],
      minute: warning,
    },
    {
      label: "The outcome",
      kicker: "04 / CHECK THE RESULT",
      title: "The record did not show recovery. The incident reopened.",
      copy: "The final temperature check failed. The saved trajectory did not simulate what would happen after rerouting, so it cannot prove that the intervention worked. The system kept the failed result and reopened the incident instead of inventing a recovery.",
      callout:
        "An action being recorded is not the same as the cargo being safe.",
      boundary:
        "This limitation is part of the demonstration, not a claimed operational success.",
      sources: [13],
      minute: 239,
    },
  ];
}
export function readStudyLocation(hash) {
  const parameters = new URLSearchParams(String(hash).replace(/^#/, ""));
  const caseId = parameters.get("case") === "sensor" ? "sensor" : "compressor";
  const raw = parameters.get("chapter");
  const chapter = raw !== null && /^[0-3]$/.test(raw) ? Number(raw) : 0;
  return { caseId, chapter };
}
export function studyHash(caseId, chapter = 0) {
  if (
    !["compressor", "sensor"].includes(caseId) ||
    !Number.isInteger(chapter) ||
    chapter < 0 ||
    chapter > 3
  )
    throw Error("Invalid study location");
  return "#case=" + caseId + (chapter ? "&chapter=" + chapter : "");
}
