---
name: incremental-literature-alerting
description: 对组织方本地导入快照做逐窗文献增量追踪：读取 case.json/task.md 与随附的 Crossref/EuropePMC/CSV/JSONL/XML 记录，按 DOI→arXiv→PMID→标题+年份归一化去重，比较相邻快照得出新增、预印本→正式版等版本更新与撤稿，审计窗口连续性并保留可回放水位，一次运行产出 incremental_state.json、report.md、provenance.json、records.csv 与契约齐全的 result.json；不联网、不声称实时或全球最新收录。
---

# 最新文献发现与增量追踪

## 1. 用途

给定若干**组织方本地导入快照**（import states），回答"相对上一次快照，这个窗口真正新增了什么"：
新增文献、版本更新（预印本→正式版→修订）、撤稿/更正、已见条目、窗口缺口。

不做：实时抓取、全文精读、自动写综述、把提醒当科学结论。**不联网**，全部事实来自 `/input`。

## 2. 输入

| 位置 | 说明 |
|---|---|
| `/input/case.json` | 必读。`case_id`、`output_contract`、`topic`/`subscription`、`window`，部分案例还带 `previous_snapshot`。 |
| `/input/task.md` | 任务说明与限制，用于核对主题与覆盖范围。 |
| 其余材料 | 快照文件（含 `snapshot_id`/`window`/`record_ids`）、`records.csv`、Crossref `message.items`、EuropePMC `resultList.result`、DataCite `data[]`、JSONL/XML 记录与论文文本。以实际文件为准，标签与文件名不作为事实。 |

## 3. 运行（单次运行即完成交付）

```bash
python /skills/weekly-skill/scripts/sk_entry.py --input /input --work /app/work --out /app/output
```

`scripts/run_skill.py` 为等价薄封装。执行后 `/app/output` 必须包含：

| 产物 | 内容 |
|---|---|
| `incremental_state.json` | 订阅、快照、逐窗增量（新增/版本更新/撤稿及依据）、窗口问题、去重键规则、已见键、版本链、水位 |
| `report.md` | 人读报告，逐条带文件与行/指针定位 |
| `provenance.json` | 记录 / 快照 → 源文件定位映射，含已用与未用来源 |
| `records.csv` | 归一化后的平面记录表 |
| `search_ir.json` | 可回放的中间表示（来源、快照、记录） |
| `validation.json` | 自检项与判定 |
| `result.json` | `case_id` + 全部 `output_contract` 键 + 状态、来源、不确定性、变更记录；契约键的值指向对应产物文件 |

## 4. 计算规则

1. **快照**：按 `window.start`、`imported_at` 排序；第一个快照是基线，只定义"已见键"，不当作新发现推送。
2. **去重键**：归一化 DOI → arXiv（去版本号）→ PMID → 归一化标题+年份。同一键跨快照再次出现不是新增。
3. **版本更新**：同键但记录 ID 不同、或刊名/载体类型（预印本→期刊）变化、或显式 `version_of`，记为 `version_update`，并保留前序定位。
4. **撤稿**：`retracted=true` 或撤稿声明标题，单独归入 `retractions`，绝不作为新论文推送。
5. **窗口**：相邻窗口首尾相接才算连续；缺口/重叠写入 `window_issues` 并给出处置（回补或按去重键去重），不静默放宽。
6. **提醒**：`retraction`(high) → `version_update`(medium) → `new_item`(normal)；同一实体只提醒一次；重复、已见、超预算条目进入 `suppressed` 并注明原因。
7. **水位与幂等**：水位=最近窗口止 + 已见键集合；以同一批快照重跑得到完全相同增量，不会二次推送。

## 5. 异常处理

| 情形 | 处置 |
|---|---|
| 缺 `case.json` 或无记录 | `status=insufficient_input`，`uncertainty` 说明，仍写 `result.json`，退出码 0 |
| 快照引用了不存在的记录 ID | 记入 `unresolved_record_ids` 与 `uncertainty`，不补造记录 |
| 空窗口（无增量） | `new=0`、`alerts=0`，`status=success`，不制造条目 |
| 日期/时区缺失 | 按墙上时间比较，缺时区留空，不猜测 |
| 字段改名/嵌套变化 | 走别名与嵌套探测；仍读不到写入 `uncertainty`，指出文件与字段 |
| 输出目录不可写 | stderr 报错并非 0 退出，不静默成功 |

## 6. 自检

- [ ] `result.json` 含 `case_id` 与 `output_contract` 每个键，值指向真实产物；
- [ ] `incremental_state.json` / `report.md` / `provenance.json` 均已生成且非空；
- [ ] 每条新增/更新/撤稿都带 `evidence.file` + `locator`；
- [ ] 基线快照未被计入新增；撤稿未混入新增；
- [ ] 窗口问题与未解析 ID 已显式登记；
- [ ] 全文无"实时/全球最新覆盖"表述。

## 7. 参考

- `references/tracking-protocol.md` —— 快照、窗口、六类差分与水位
- `references/dedup-and-versioning.md` —— 去重键与版本链
- `references/evidence-and-replay.md` —— 证据定位、契约映射、幂等回放
- `references/failure-modes.md` —— 失败模式与退路
- `assets/policy.json` —— 去重优先级、提醒预算与限制
- `examples/demo-case/` —— 合成演示（虚构 DOI），可用 `--input` 直接跑通
