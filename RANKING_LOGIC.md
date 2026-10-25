# Ranking logic (the deterministic "fake" model)

When run with `--fake-llm` / `LLM_FAKE=1`, nvidia-maxxing uses no AI to pick the culprit. It uses a points system over the evidence. The same ranking is also handed to the real LLM as a starting point.

Code: `maxxing/evidence/rank.py` (`classify`, `rank_commits`) and `maxxing/agent/fake.py` (`heuristic_report`).

## 1. Candidate commits

Every commit between the last healthy deploy (from `.deploys.json`, or the `known-good` tag) and the **deployed** commit is a candidate. Commits made after the deploy are not running, so they are ignored. Each commit carries:
- its diff
- the files it changed
- the line ranges it changed in each file

Note: the deployed commit is just the newest code running. It is not necessarily the cause.

## 2. Classify the outage

`classify()` reads hard signals from the evidence. The first matching rule wins:

| # | Signal | Category |
|---|---|---|
| 1 | A dependency container (redis, postgres, ...) is down | `dependency` |
| 2 | OOMKilled, exit 137, memory ≥ 85% of the limit, or a V8 heap error | `resource` |
| 3 | Connection errors (ECONNREFUSED, ClientOfflineError, ...) **and** a config commit in the window | `config` |
| 4 | Stack traces with frames in repo source files | `code` |
| 5 | Health checks timing out, or 504s from the LB | `resource` (CPU) |
| 6 | Connection errors with no config change | `dependency` |

## 3. Score every commit

`rank_commits()` adds up points per commit:

| Evidence | Points |
|---|---|
| Touches only docs/tests/non-runtime files | −3 |
| Touches runtime code | +0.5 |
| Diff changes the crashing line (±2 lines) | +4 × weight |
| Diff only touches a file in the stack trace | +1.5 × weight |
| `git blame` on the crashing line points at this commit | +6 |
| Diff contents match the signal type | +3, plus 1.5 per extra match, capped at +6 |
| Recency tiebreak (newer commits) | up to +0.2 |

The weight grows with how often the error occurs, up to 2×. Errors from a dependency (connection errors) are skipped for the stack-trace checks, because where they were thrown says nothing about the culprit.

What "matches the signal type" means:
- **Memory pressure:** the diff adds `new Map(`, `cache`, `Buffer.alloc`, `.push(` or `setInterval`.
- **CPU pressure:** the diff adds `*Sync(`, `pbkdf2`, `bcrypt`, busy loops or heavy JSON work.
- **Connection errors:** the diff changes a host, port, `REDIS_*` or `process.env` default, or touches a config file (`config.js`, `.env`, `docker-compose.yml`, `nginx.conf`).

## 4. Pick the culprit

`heuristic_report()` only blames the top commit if all of these hold:
- its score is at least 3
- it beats the runner-up by at least 1
- the category is not `dependency`

Otherwise it reports "no culprit" and blames nobody.

The proposed fix is the culprit's diff inverted, which amounts to a revert.

## Example: scenario 4 (bad config)

- **Signal:** the logs show `ClientOfflineError`, a connection error, so the outage is classified `config`.
- **`a4f3789`** changes `src/config.js`, editing the Redis port and a `process.env` default. It scores +0.5 for runtime code plus about +6 for config and connection, so it is the clear winner.
- **`118207a`** (the footer decoy) only changes `index.html`. It scores +0.5 for a runtime file plus the recency tiebreak, about 0.6.

## Fake mode vs the real LLM

With `LLM_FAKE=1`, the real agent loop still runs, but a scripted client plays the model. It makes a couple of tool calls, then returns the report above.

With Nemotron, the ranking and its reasons become a starting point. The model reads the actual diffs, can confirm or overrule the ranking, and writes a real fix.

The rules are only as good as their keyword lists. A novel bug that matches no pattern would beat the fake model, but not necessarily the LLM.
