# Runtime contract

Bohrium Playground Period 4 submission — challenge `challenge-1e85736f` (track: `literature-search`).

This repository holds the complete skill package for the challenge: the skill
definition, its executable entry point and all supporting scripts. No precomputed
answers, private data or credentials are included.

## Skill

- Name: `incremental-literature-alerting`
- Summary: Use when a research task needs a repeatable literature-tracking loop over fixed time windows: snapshot watermarks, incremental new-item diffing, dedup and version merging, retraction/correction handling, suppressible alerts, and an idempotently replayable result.json that preserves case_id and output_contract fields.

## Contents

| File |
|------|
| `SKILL.md` |
| `scripts/run_skill.py` |
| `README.md` |

## How to run

```bash
python scripts/run_skill.py --case /input/case.json --output /app/output/result.json
```

The entry point is `SKILL.md`; input/output contracts, error handling and a minimal
usage example are documented there.

## Notes

- Pure Python standard library; no network access required.
- The result JSON keeps the input case id, the main results, per-item evidence,
  a run status and an uncertainty section.
