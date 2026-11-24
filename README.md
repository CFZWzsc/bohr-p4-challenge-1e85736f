# Runtime contract

Bohrium Playground Period 4 submission — challenge `challenge-1e85736f` (track: `literature-search`).

This repository holds the complete skill package for the challenge: the skill
definition, its executable pipeline and all supporting documents. No precomputed
answers, private data or credentials are included.

## Skill

- Name: `incremental-literature-alerting`
- Summary: 当任务要求对固定时间窗做最新文献发现与增量追踪时使用：解析订阅与窗口、递归读取 Crossref/EuropePMC/DataCite/XML/JSONL/CSV 记录、按 DOI→PMID→arXiv→标题+年份去重、合并预印本/正式版/修订版本链、与上一次快照差分出新文献/版本更新/撤稿/已见/窗外/索引滞后、按规则抑制提醒，并输出可幂等回放且完整覆盖运行时 output_contract 的 result.json 与证据索引。

## Contents

| File |
|------|
| `SKILL.md` |
| `assets/policy.json` |
| `examples/demo-case/case.json` |
| `examples/demo-case/crossref.json` |
| `examples/demo-case/europepmc.jsonl` |
| `examples/demo-case/expected.json` |
| `examples/demo-case/preprint.json` |
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
