---
name: outage-triage
description: Answer follow-up questions about production outages of very-important-project (why it broke, which commit, who, how to fix, is it resolved) by querying the local nvidia-maxxing triage bot. Use when someone asks about an incident, an INC-<n> id, a culprit commit, error logs, or wants a fresh triage.
metadata: {"openclaw": {"emoji": "🚨", "requires": {"bins": ["curl"], "env": ["MAXXING_API_URL"]}}}
---

# Outage triage (nvidia-maxxing)

nvidia-maxxing watches the `vip` service. When it goes down, the bot records an incident (INC-<n>)
with a root cause, the culprit commit and its author, and a proposed fix. This skill lets you
answer follow-up questions from that data. The triage bot runs on this machine and uses the local
Nemotron model, so logs and code never leave the box.

You run inside the OpenShell sandbox, without the Docker socket or the repo. Talk to the bot's
read-only HTTP API with `curl`:

- **Base URL:** `$MAXXING_API_URL`.
  - Inside OpenShell, the default is `http://host.openshell.internal:7070`.
  - When OpenClaw runs directly on the host (not sandboxed), use `http://localhost:7070`.
- **Auth:** if `$MAXXING_API_TOKEN` is set, add `-H "Authorization: Bearer $MAXXING_API_TOKEN"` to every call.

```bash
API="${MAXXING_API_URL:-http://host.openshell.internal:7070}"
AUTH=(); [ -n "${MAXXING_API_TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $MAXXING_API_TOKEN")
```

## Which call to use

| The user wants… | Call |
|---|---|
| recent / open incidents | `curl -s "${AUTH[@]}" "$API/incidents?limit=5"` |
| details of INC-12 (RCA, culprit, fix, evidence, timeline) | `curl -s "${AUTH[@]}" "$API/incidents/12"` |
| a reasoned answer ("why not commit abc?", "show me the leak", "is redis the problem?") | `curl -s "${AUTH[@]}" -X POST "$API/ask" -H 'Content-Type: application/json' -d '{"question": "<question>", "incident_id": 12}'` (leave out `incident_id` to use the most recent incident) |
| a fresh analysis right now ("is it still broken?", "triage again") | `curl -s "${AUTH[@]}" -X POST "$API/triage"`. This is slow (it runs the LLM), so tell the user it can take up to a minute. |
| check that the bot is up | `curl -s "$API/health"` |

Build the `/ask` JSON with `jq -n --arg q "$QUESTION" '{question: $q}'` when the question contains
quotes. Every response is JSON: `/ask` returns `{"answer": "..."}`, and `/incidents/{id}` returns
`report` (summary, category, root_cause, culprit {sha, confidence, why}, fix {explanation, diff},
remediation[]), `evidence` (containers, error_clusters, commits, candidates) and `timeline`.

## How to answer

1. Prefer `/ask` for "why" and "how" questions. It runs the local model with access to logs,
   container state, diffs and blame. Use `/incidents/{id}` for factual lookups such as the sha, the
   author, the time and the status.
2. Quote commits by their 8-character sha and author, for example `722ae88f` (Marcus Lee). Put
   diffs in ```diff fences, and keep replies under about 15 lines.
3. **Never blame a person the bot did not name.** If `culprit` is null (for example a dependency
   outage such as redis down), say so plainly and point to the dependency.
4. Do not offer to roll back from here. This skill is read-only; suggest the revert command from the
   report's next steps instead.
5. If the API is unreachable, say that the triage bot looks down (`maxxing run --serve` or
   `maxxing serve` on the host). Do not guess.

## Fallback: running unsandboxed on the host

If you have a shell on the GB10 itself, with the repo and the Docker socket, the same logic is
available as a CLI from the `nvidia-maxxing` directory:

```bash
uv run maxxing ask "why did INC-12 happen?" --incident 12
uv run maxxing triage --now --json     # fresh RCA as JSON
```
