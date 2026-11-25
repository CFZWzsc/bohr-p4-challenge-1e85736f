# Runtime contract

Bohrium Playground Period 4 submission — challenge `challenge-1e85736f` (track: `literature-search`).

This repository holds the complete skill package for the challenge: the skill
definition, its executable pipeline and all supporting documents. No precomputed
answers, private data or credentials are included.

## Skill

- Name: `incremental-literature-alerting`
- Summary: 对组织方本地导入快照做逐窗文献增量追踪：读取 case.json/task.md 与随附的 Crossref/EuropePMC/CSV/JSONL/XML 记录，按 DOI→arXiv→PMID→标题+年份归一化去重，比较相邻快照得出新增、预印本→正式版等版本更新与撤稿，审计窗口连续性并保留可回放水位，一次运行产出 incremental_state.json、report.md、provenance.json、records.csv 与契约齐全的 result.json；不联网、不声称实时或全球最新收录。

## Contents

| File |
|------|
| `SKILL.md` |
| `assets/policy.json` |
| `examples/demo-case/case.json` |
| `examples/demo-case/expected.json` |
| `examples/demo-case/import_state_1.json` |
| `examples/demo-case/import_state_2.json` |
| `examples/demo-case/records.csv` |
| `examples/demo-case/task.md` |
| `examples/quickstart.md` |
| `references/dedup-and-versioning.md` |
| `references/evidence-and-replay.md` |
| `references/failure-modes.md` |
| `references/tracking-protocol.md` |
| `scripts/run_skill.py` |
| `scripts/sk_entry.py` |
| `README.md` |

## How to run

```bash
python scripts/sk_entry.py --input /input --work /app/work --out /app/output
```

A single deterministic pass reads `/input`, computes the deliverable and writes a
contract-complete `/app/output/result.json` plus the human-readable review files
described in `SKILL.md`. The same input always produces byte-identical output; no
network access is used, and fields the materials cannot support are reported
explicitly instead of being left empty.

## Notes

- Pure Python standard library; no network access required.
- Every claim is anchored to paragraph-level evidence; unknown items are reported
  in `uncertainty` instead of being fabricated.
