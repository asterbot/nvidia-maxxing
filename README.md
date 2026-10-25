# nvidia-maxxing
Servers? Ain't no thang.

nvidia-maxxing is an autonomous outage triage agent that runs entirely on a **Dell Pro Max GB10**.

When production goes down, it:
1. detects the outage
2. pulls the error logs, container state and the commits deployed since the last healthy release
3. has a **local Nemotron** model (through Ollama's OpenAI-compatible API) investigate with tools
4. prints a root-cause analysis to the terminal: the culprit commit and its author, why it's to blame, and a proposed fix as a diff.

When the service recovers, it prints "Resolved in Xm".

No remote LLM calls are made. Logs and source code never leave the box.

## Why it matters

- **MTTR.** Most outage time is spent on triage: finding the right logs, working out what changed, and finding who knows that code. nvidia-maxxing has an RCA, a suspect commit and a revert ready about 30s after detection, and the on-call engineer starts from the answer.
- **On-call cost.** Fewer people get woken up to dig through logs. The person whose change broke things is named, with the diff in hand. A dependency or infra outage pages *nobody's* commit: the bot says "no culprit" rather than blaming an innocent engineer.
- **Local-first.** Production logs and proprietary code are the data you least want to send to a third-party API. Here the whole loop runs on the GB10: detector, evidence and Nemotron. Nothing leaves the box.

## Architecture

```
 traffic ─► nginx lb :8080 ─► api ×3 (Express, 256m / 0.5 cpu) ─► redis          very-important-project
                 │  pino JSON + nginx access logs, docker events, /health
 ════════════════╪═══════════════════════════════════════════════════════════════════════════════════
                 ▼                                                                 nvidia-maxxing
 ┌─ Detector (asyncio) ─────────────┐   ┌─ Evidence collector (deterministic) ──────────────────┐
 │ health.py   LB /health every 2s  │   │ containers.py inspect: OOMKilled, ExitCode, restarts, │
 │ docker_events.py die/oom/health  │──►│               health log, mem vs limit, cpu           │
 │ log_tail.py rolling 5xx + fatals │   │ logs.py       window, error clusters by stack sig     │
 │ incident.py rule engine: dedup,  │   │ git.py        known-good (.deploys.json) .. deployed, │
 │   cooldown, deploy grace, recover│   │               diffs, hunks, blame of crashing lines   │
 └──────────────────────────────────┘   │ rank.py       heuristic prior + category hint         │
                                        └──────────────────────┬────────────────────────────────┘
                                                               ▼ EvidenceBundle (pydantic, JSON-dumpable)
 ┌─ RCA agent (agent/rca.py) ────────────────────────────────────────────────────────────────────┐
 │ local Nemotron via OpenAI-compatible API (Ollama :11434/v1). Tool loop of up to 8 steps:      │
 │ get_logs · get_container_state · list_commits · show_commit · read_file · grep_repo · blame   │
 │ → RCAReport (pydantic) · repair-retry on bad JSON · culprit must be a candidate or null       │
 │ · single-shot fallback for models without tool calls · heuristic fallback if the model is down│
 └──────────────────────────────────────────────┬────────────────────────────────────────────────┘
                                                ▼
 ┌─ Console report (report.py) ───────────┐   ┌─ SQLite incident store ─┐   ┌─ HTTP API :7070 ────────┐
 │ severity, impact, root cause,          │◄─►│ incidents, timeline,    │◄─►│ GET /incidents[/id]     │
 │ culprit commit + author, fix diff,     │   │ bundle + report JSON    │   │ POST /ask  POST /triage │
 │ next steps; --details: timeline,       │   └─────────────────────────┘   └──────────▲──────────────┘
 │ commit ranking, agent notes            │                                             │ curl
 │ "Resolved in Xm" on recovery           │                    OpenClaw / NemoClaw in OpenShell sandbox
 └────────────────────────────────────────┘                    (skills/outage-triage) answers follow-ups
```

The design rests on two ideas:
- **The pipeline is deterministic and the LLM does the reasoning.** Detection and evidence collection never depend on the model, so the demo can't stall there. If the model is unreachable or returns garbage, the bot still prints a heuristic report and labels it as one.
- **Ranking happens in two stages.** First, `rank.py` scores every commit in the window. It uses:
  - stack-frame overlap with diff hunks
  - `git blame` of the crashing line
  - resource-signal keywords: unbounded `Map` / `Buffer.alloc` when a container is OOMKilled, `*Sync` crypto when health checks time out
  - config changes when there are connection errors
  - a penalty for docs/test-only commits

  Then Nemotron reads the diffs of the top candidates and confirms or overrules the prior.

Outage categories are separated using hard signals:

| Category | Signals |
|---|---|
| `resource` | OOMKilled or exit 137, memory near the limit, health checks timing out, 504s |
| `code` | app stack traces, crash loops with exit 1 |
| `config` | connection errors plus a config change in the window |
| `dependency` | a dependency container is down, or connection errors with no relevant change. The culprit is always null. |

## Setup on the GB10

```bash
# 1. Local model (Ollama on the GB10; NIM/vLLM work too, since only the OpenAI-compatible API is used)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nemotron-3-super          # or a smaller Nemotron Nano tag if latency matters
curl -s localhost:11434/v1/models     # sanity check

# 2. This project
curl -LsSf https://astral.sh/uv/install.sh | sh
cd nvidia-maxxing && uv sync
cp .env.example .env                  # set LLM_MODEL if not nemotron-3-super

# 3. The victim (see ../very-important-project/README.md)
cd ../very-important-project && git tag known-good && scripts/deploy.sh && scripts/seed.sh
```

## Usage

```bash
uv run maxxing run                    # daemon: detector -> evidence -> Nemotron -> report in the terminal
uv run maxxing run --details          # ...plus timeline, commit ranking and agent notes
uv run maxxing run --serve            # ...and the read-only HTTP API on :7070 in the same process
uv run maxxing serve                  # API only (pair it with `maxxing run` in another terminal)
uv run maxxing triage --now           # one triage pass now, printed to the terminal
uv run maxxing triage --now --dump tests/fixtures/mine.json   # save evidence as a replay fixture
uv run maxxing triage --fixture tests/fixtures/2-null-deref.json
uv run maxxing ask "why not blame commit 7659b878?"
uv run maxxing replay tests/fixtures/*.json   # score the agent over saved bundles (no Docker)
uv run maxxing eval                   # break the service 6 ways and print a scorecard
```

Global flags:
- `--fake-llm` (or `LLM_FAKE=1`): a deterministic fake model built on the heuristic ranking. It still drives the real tool loop, so the whole pipeline runs without a model.

`maxxing run` and `maxxing serve` share the same SQLite store, so you can run them together. `maxxing run --serve` is the simplest option for the demo.

### HTTP API (for sandboxed agents)

The NemoClaw/OpenClaw agent runs inside the OpenShell sandbox without the Docker socket or the repo. It reaches the bot over a small read-only API. Each endpoint calls the same service functions as the CLI.

| Method | Path | Returns |
|---|---|---|
| GET | `/health` | `{"ok": true, "model", "llm_fake"}` |
| GET | `/incidents?limit=N` | recent incidents (id, status, summary, category, culprit, ...) |
| GET | `/incidents/{id}` | full `RCAReport`, evidence summary and timeline |
| POST | `/ask` `{"question", "incident_id"?}` | `{"answer"}` (same logic as `maxxing ask`) |
| POST | `/triage` | runs `triage --now` and returns the report |

Set `MAXXING_API_TOKEN` to require `Authorization: Bearer <token>` on every endpoint except `/health`. The OpenClaw skill in `skills/outage-triage/SKILL.md` calls this API with `curl` at `$MAXXING_API_URL`:
- sandboxed default: `http://host.openshell.internal:7070`
- running on the host: `http://localhost:7070`

## Testing

There are three layers, from fastest to most realistic:

1. **Unit tests (`uv run pytest`):** no Docker and no model. They cover:
   - incident rules: thresholds, dedup, cooldown, recovery, deploy grace, "a healthy surge with 404s doesn't page"
   - error clustering
   - `rank.py` over synthetic git repos built in `tmp_path`
   - RCAReport parsing and the repair-retry path
   - the guards: unknown SHA is dropped, no blame on dependency outages
   - the tools-unsupported fallback
   - the console report (culprit and author shown; no blame on dependency outages)
   - the HTTP API handlers
2. **Replay:** `tests/fixtures/*.json` are evidence bundles for scenarios 1–5, with matching `*.expected.json` ground truth.
   - `test_replay_fake_llm` always runs.
   - `uv run pytest -m llm` runs the same fixtures against the local model and skips if none is reachable.
   - The fixtures are built by `tests/fixtures/build_fixtures.py`. It replays the real scenario edits on the real sources and pushes hand-written logs, in the service's exact log formats, through the real evidence pipeline.
   - To use real captures from the GB10 instead, run `maxxing eval --dump-dir tests/fixtures`.
3. **End to end:** `maxxing eval`. For each scenario it:
   - runs `scenarios/reset.sh`
   - waits for a healthy baseline
   - starts a fresh detector
   - runs the scenario
   - waits up to 60s for an incident and an RCA
   - scores the result against `.scenario.json`

   It then prints a scorecard: detected, culprit, category, time to detect, time to RCA. Scenarios 5 (redis down: no blame) and 6 (healthy surge: no incident) are the negative controls.

## Layout

```
maxxing/
  config.py              env / .env settings
  detector/              health.py, docker_events.py, log_tail.py, incident.py (rule engine), runner.py
  evidence/              containers.py, logs.py, git.py, rank.py, collector.py, models.py (EvidenceBundle)
  agent/                 llm.py, tools.py, prompts.py, rca.py (tool loop), schema.py (RCAReport), fake.py, ask.py
  report.py              console RCA report + the notifier hooks the pipeline calls
  incidents/store.py     SQLite incident store
  service.py             triage_now / ask, shared by the CLI, API and daemon
  pipeline.py            detector -> evidence -> agent -> console report
  api.py                 read-only HTTP API (aiohttp)
  eval.py                end-to-end scenario harness + scorecard
  cli.py                 typer CLI
skills/outage-triage/    OpenClaw skill
tests/                   pytest + fixtures
```
