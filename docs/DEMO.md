# Running the demo

Written for: whoever is driving the screen. Assumes nothing is running.

---

## Before the room

Five commands, in this order. Total about three minutes, most of it Docker.

```bash
cd /c/dev/axonfde

# 1. Docker Desktop must be running first. It is not on PATH:
#    C:\Users\Dilip\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe
docker compose --profile core up -d

# 2. Schema, the restricted runtime role, and the seeded ERP.
uv run poe migrate
uv run poe seed

# 3. The scenario recordings the detectors read. Gitignored, so this is
#    required on a fresh checkout.
uv run poe forge run-all

# 4. Run the loop and record its trace. This is what the UI displays.
uv run poe demo-trace

# 5. Serve it. Check port 8000 first (see "If something is broken" below) -
#    on this machine it has been taken by other projects in every session
#    since 2026-09-26. If it is, pick another port:
uv run poe tower          # http://localhost:8000
uv run poe tower --port 8012   # when 8000 is taken
```

**Check before you present:** the terminal from step 4 must end with
`Rolled back.` and no red. If the loop failed, no trace is written and the UI
will say so rather than show you a stale one — which is deliberate, but not
something to discover in front of an audience.

`poe demo-trace` rolls its transaction back, so it can be run again any number
of times. Run it once more right before the room if anything has been touched.

---

## What to show, in order

### 1. The chart — 30 seconds

Two vertical lines. The solid cyan one at **minute 102** is AxonFDE. The dashed
red one at **minute 137** is where the cargo first leaves the 2–8 °C envelope,
which is where the customer's existing threshold alarm fires. (Cyan and red,
not green and red: those two collapse under colour-vision deficiency, which is
why the redesign never pairs them.)

> "Thirty-five minutes of warning on this shipment. Every reading before 137 is
> in spec, so a threshold alarm shows green the whole way."

The line is **what the sensor reported**. The simulator knows the true
temperature and the API deliberately will not serve it — there is a test that
fails if it ever does.

### 2. The claim cards — 60 seconds, and this is the part that lands

Four measured claims, each with the run id that produced it.

Do not quote the 49-minute median on its own. The card will not let you: it
prints the false-alarm rate in the same box, because `claims.md` says an
unpaired lead time is a misuse of the claim.

> "Median lead time across forty breach scenarios is 49 minutes, at a 30%
> false-alarm rate against the threshold alarm's 20%. Ten points of extra false
> alarms is the price."

Then the caveat under the cards, which is the strongest thing on the page:

> "On 23% of those scenarios — nine of forty — the detector fired *before the
> fault started*.
> In hot ambient the cargo genuinely climbs while the unit settles, and linear
> extrapolation can't tell that curve from an excursion. The honest median over
> alerts that followed their fault is 35 minutes. We found that by widening the
> scenario pack from 3 to 60 — and we didn't tune it away, because retuning a
> parameter against the benchmark that measures it is how a number stops
> meaning anything."

If you say nothing else, say that.

### 3. "Two shipments that look the same" — 60 seconds, the sophisticated beat

Two scenarios, four sparklines. Both cargo temperatures climb; the **lying**
sensor climbs faster (+0.039 °C/min against +0.016).

> "From temperature alone these are the same picture, and the fake one looks
> worse. What separates them is the compressor: one is winding *down* at
> −9.2 rpm/min while the cargo warms — a unit losing the fight. The other is
> winding *up* at +2.0 while the reading races, which is physically incoherent,
> so the instrument is what's wrong. The second one also reports no fault code."

Then the punchline, which is the bit that shows measurement discipline:

> "Both of those are single-modality telemetry. So our multimodal ablation's
> *baseline* arm has to include compressor response — otherwise we'd credit a
> photograph with a discrimination telemetry already made, and the claim would
> be inflated. We wrote that into the register before building the ablation."

Every number on that panel is computed in the browser from the served
telemetry, over the same 30-reading window `claims.md` uses. They match the
register because they are the same calculation, not because they were copied.

### 4. The timeline — 90 seconds

Thirteen steps, no language model anywhere in them. Three steps carry a
**refusal** and they are the point of the whole system:

| Step | What is refused | Why it matters |
|---|---|---|
| Approval requested | The AI cannot act on its own | Policy matrix, 50 role × action cells, zero bypasses |
| `APPROVAL_STALE` | 48 new readings arrived after the approver looked | The approval is bound to the evidence, the risk probability *and* its baseline |
| Execution | Runs only against a valid, unexpired, hash-matching approval | Invariant I5 |

The last step verifies the outcome, finds the intervention did **not** work,
and reopens the incident. That is honest: the recording is the trajectory of a
truck nobody rerouted.

### 5. The LLM showcase — 60 seconds, right after the loop finishes

The terminal prints a fifth, un-numbered section: the same flagship incident,
investigated by the real two-model-node graph, replayed from a committed
cassette (free, offline, deterministic — nothing is called live). Point at
three things in order:

1. **The hypothesis list.** Each cause shows a *confidence* and the *rule
   prior* it is capped by — `compressor_degradation` reads confidence 0.78
   against a prior of 0.80. The model narrowed belief inside what the rules
   already permitted; it never exceeded the prior. That is invariant I2, on
   screen.
2. **The citations** — on the hypothesis lines (`cites E03, E17, E18`), not in
   the narrative. The narrative prints as plain prose with no `E` codes in it,
   so do not point at it for citations; every handle on the hypothesis lines
   resolves back to the evidence bundle. Then read one sentence of the
   narrative aloud — its figures (7.15 C, 1488 RPM, 1800 USD) are what the
   next line checks.
3. **The grounding line** — `grounded: 12 citations and 10 figures all
   supported`. This is a deterministic check, not another model call: every
   citation is checked against the evidence bundle and every number against a
   computed value.

Say the caveats before anyone asks: this is one illustrative incident, not a
claim. `openai/gpt-oss-120b`/`-20b` on Groq's free tier, not a frontier model —
chosen because it needed no paid account. The measured numbers, over all 44
investigated incidents, are C3/C5/C10/C12 in `claims.md`, and C5 is
**refuted**: this small model's root-cause accuracy is *worse* than the
deterministic rules. Lead with that. A negative result you found and measured
yourself is a stronger signal than a cherry-picked win — it is proof the
benchmark isn't decorative.

---

## Questions you should expect

**"Is any of this real?"**
The physics is a documented lumped-capacitance thermal model and the README
says so at every point of use. The databases are real — Postgres and SQL
Server, in containers. The loop is the shipped code path, not a script written
for the demo: the UI and the terminal run the same function.

**"Where's the LLM?"**
Section 5, right after the loop closes — same incident, real graph, replayed
from a committed cassette. It is deliberately *not* inside the 13 steps above:
those are `rules_only`, the ablation baseline the LLM is measured against, and
that measurement only means something if the baseline never changed once the
arm existed. `rules_only` has run in CI since before the model boundary did.
An ablation arm built afterwards is an argument; one that predates the
treatment is a measurement.

**"Did the LLM help?"**
Measured, and the honest answer is no — C5 is `REFUTED`. Root-cause accuracy
dropped (0.500 → 0.432); the correct action was **identical on all 44
incidents**, because the decision engine never reads a hypothesis (invariant
I2, confirmed on real output, not just asserted); the one place it helped was
judged explanation quality (1.8 → 3.3 of 5). That result is for a small free
model, and is explicitly not a verdict on `gpt-5` or `claude-sonnet-5` — say so.

**"What did you get wrong?"**
Offer these without being asked; they are the most credible thing you have.
- The app was connecting as a **superuser**, so the append-only grants on the
  audit table were silently inert.
- Concurrent audit appends don't fork the chain — they **lose events**, which
  for an audit log is worse, and a fork is at least visible.
- CI had failed on **every run since the first commit** and nobody noticed,
  because `poe check` was green locally.
- A test written to catch a reproducibility bug **passed against that bug**.
- The detector false-alarm mode above, found by our own benchmark.

**"Can I see it fail?"**
`uv run poe test-sec` — the security suite. Or break something: the tests that
protect load-bearing behaviour have all been verified by breaking the code and
watching them go red.

---

## If something is broken on the day

| Symptom | Cause | Fix |
|---|---|---|
| UI says "no closed-loop run has been recorded" | step 4 not run, or it failed | `uv run poe demo-trace` |
| UI says "no recording for …" | `data/generated/` is gitignored | `uv run poe forge run-all` |
| `LegacyConnectionError … 1433` | Docker Desktop died | restart it, then `docker compose --profile core up -d` |
| Claim cards empty | no published benchmark run | `uv run poe bench --arm rules_only`, then copy into `benchmarks/results/published/` |
| Demo step 1 fails on the ERP | not seeded | `uv run poe seed` |
| `localhost:8000` shows `{"detail":"Not Found"}` (or a page that isn't AxonFDE), yet `/docs` works | **something else is already on port 8000.** uvicorn logs `error while attempting to bind`, shuts down and `poe tower` exits with code 3 — easy to miss if the terminal is behind the browser. The browser is talking to the *other* process, whose own `/docs` answers 200 | `uv run poe tower --port 8012`, or free the port (below) |

Finding what has port 8000, on Windows:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  ForEach-Object { Get-Process -Id $_.OwningProcess } |
  Select-Object Id, ProcessName, StartTime
```

A stale `python` from an earlier session is the usual answer; on 2026-09-28 it
was a `job-hunt-os` uvicorn **and** a `wc26-mlops-serving` container that
Docker Desktop restarts on launch, so also check
`docker ps --format "{{.Names}} {{.Ports}}"`. This has already
happened once before: `poe tower` failed to bind, an older server answered `/` and
`/docs` with 200, and the control endpoints returned `{"detail":"Not Found"}`
— which reads exactly like a broken router rather than a busy port.

Docker dying mid-session has happened in four of the last six working sessions.
Check it first.
