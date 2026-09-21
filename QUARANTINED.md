# QUARANTINED — Do not use as pipeline Stage C

**Status:** Quarantined for Herbenzo glue / export-trust demos (Architecture Audit Finding #4).

## Why

This Desktop **base** tree (`~/Desktop/dossier_engine` on `desktop-v2`) wires
`SafetyQueryNode` **without** `ClaudeAuthenticator`. Safety §3 can cite
keyword PubMed hits that are real papers but off-target for the claim.

The **only** runnable pipeline C is:

```
~/Desktop/dossier_engine-harden-c
branch: harden-c (or cursor/task-t1-quarantine-c-*)
python -m dossier_engine.webserver   # port 8010
GET /api/health → safety_adjudication: "required"
```

## What still works here

Local exploration, offline `example_run`, and reading code. The webserver
prints a hard warning on startup and **refuses** when `PIPELINE_MODE=1`
(or `DOSSIER_PIPELINE=1`). Health reports `pipeline_ready: false` and
`safety_adjudication: "absent"` so glue rejects this build.

## Operator rule

Point scripts, glue `C_URL`, and docs at **harden-c only**. Keep C’s
independent UI; do not merge stage UIs.

## Secrets trap (`.env.txt`) — Audit #23 / Task T24

This base tree historically held a raw-blob `.env.txt`. **No Herbenzo runtime
loads `.env.txt`.** Pipeline C (`dossier_engine-harden-c`) reads only `.env`
as `KEY=value`.

If you still have `.env.txt` here:

1. Do **not** point glue at this tree.
2. On harden-c: `cp .env.example .env` and paste `ANTHROPIC_API_KEY=…`.
3. Delete or leave the local `.env.txt` (gitignored); never commit it.

See harden-c README and glue `SECRETS.md`.
