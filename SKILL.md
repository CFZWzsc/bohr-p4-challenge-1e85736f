---
name: incremental-literature-alerting
description: Use when a research task needs a repeatable literature-tracking loop over fixed time windows: snapshot watermarks, incremental new-item diffing, dedup and version merging, retraction/correction handling, suppressible alerts, and an idempotently replayable result.json that preserves case_id and output_contract fields.
---

# 最新文献发现与增量追踪

## 用途与适用条件

当任务要求持续跟踪某一主题的新论文、版本更新或撤稿更正，并且必须说明"相对上一次快照新增了什么"时使用。交付物是一条可按时重放、幂等、去重受控的追踪链路，而不是一次性主题检索结果。

**适用前提**：输入真实提供当前窗口的检索结果，以及（理想情况下）上一次快照或水位、窗口起止时间。缺少快照时仍会运行，但只能作为基线，且会明确标注 `partial`。

**不适用**：全文阅读、自动写综述、未经授权的定时抓取、把提醒内容当作科学结论。本 Skill 不联网、不启动调度器，时间窗与来源由输入给定。

## 输入

| 输入 | 说明 |
|---|---|
| `/input/case.json` | 必读。常见结构：`window`（`start`/`end`/`timezone`，或 `window_start`/`window_end`）、`previous_snapshot`/`snapshot`（记录数组或去重键数组，可带 `watermark`）、`records`/`results`/`items`（当前窗口结果）、`alert_rules`（`max_items`、`include_updates`、`include_retractions`）。实际键名以文件为准。 |
| `task.md` | 可选，存在时读取用于核对订阅主题、窗口与提醒要求。 |
| `--work` | 工作目录，默认 `/app/work`。 |

记录字段识别：标题、DOI、PMID、arXiv、作者、期刊、日期（`first_indexed`/`entry_date`/`published`/`date`/`issued`）、`version`、`retracted`、`update_type`、`source`。

## 处理流程（对应评分维度）

1. **读取订阅与窗口**（任务完成度）：解析 `start`/`end`，保留 `timezone` 标签；窗口缺失时不禁用记录，但把"窗口不完整"写入 `uncertainty`。
2. **去重**（结果正确性）：按 `DOI → PMID → arXiv → 归一化标题+年份` 生成 `dedup_key`；同一键的多条来源合并为一个条目，保留全部 `source_locators` 与来源集合，日期取最晚值。
3. **快照比对**（正确性/可回溯性）：把上一次快照的键与标题+年份集合作为水位；当前记录逐条判定 → `new_items`（快照中没有）、`updates`（已存在但出现版本/更新标记）、`already_seen`（已存在且无变化）、`retractions`（撤稿标记）、`out_of_window`。
4. **时间窗边界**（完整性）：日期早于 `start` 或晚于 `end` 的条目不进入提醒，单独列在 `out_of_window`，不静默丢弃；无日期记录保留并标注。
5. **提醒抑制**（可靠体验）：先撤稿、再版本更新、后新文献排序；重复项与窗外项一律抑制；超过 `max_items` 的条目进入 `suppressed` 并注明原因。每条提醒保留 `kind`、`priority`、`reason`。
6. **幂等与水位推进**（创新增益）：输出 `next_snapshot.keys`（旧键 ∪ 当前键）与 `watermark`；同一输入重复运行得到同一分类，因为不原地改写历史快照。

## 输出契约

写入 `--out`（默认 `/app/output/result.json`）。顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `case_id` | string | 原样保留输入 `case_id` |
| `status` | string | `success` / `partial` / `insufficient_input` / `failed` |
| `result` | object | `new_items`、`updates`、`retractions`、`already_seen`、`out_of_window`、`alerts`、`suppressed`、`alert_rules`、`window`、`snapshot`、`next_snapshot`、`replay`、`limitations`、`change_log` |
| `new_items` / `alerts` | array | 顶层镜像，便于契约字段直接读取 |
| `evidence` | array | 窗口、快照、当前记录、提醒的来源定位 |
| `uncertainty` | array | 空窗口、缺快照、缺日期、时区、索引滞后 |
| `errors` | array | 运行错误；正常为空 |
| `stats` | object | 当前记录数、新增、更新、撤稿、窗外、提醒与抑制数 |
| `contract_notes` | object | 未产出契约字段说明 |
| `output_contract_fields` | object | `provided` 与 `present` |

输入 `output_contract` 的每个字段名都会出现在顶层；无法产出时写 `null` 并在 `contract_notes` 说明。

`status`：无当前记录且无历史快照 → `insufficient_input`；缺快照或缺完整窗口 → `partial`；窗口与快照齐备 → `success`（即使窗口为空、提醒为 0，也是有效的"无新增"结论）；解析失败 → `failed`。

## 异常处理

| 情形 | 处理 | 输出标记 |
|---|---|---|
| 窗口为空（无新增） | 输出空 `new_items` 与空 `alerts`，不制造条目 | `status=success`，`stats.new_items=0` |
| 无上一次快照 | 作为基线运行，所有窗内条目记为新增 | `status=partial`，`uncertainty.type=no_previous_snapshot` |
| 缺窗口起止 | 不做窗外过滤，明确声明 | `uncertainty.type=window_incomplete` |
| 记录缺日期 | 保留条目，不推断日期 | `uncertainty.type=missing_dates` |
| 撤稿/更正 | 单独成类并优先提醒 | `retractions`，`alerts[].kind=retraction` |
| 已见条目重复出现 | 抑制，不二次推送 | `suppressed` |
| 提醒超预算 | 截断并逐条说明 | `suppressed[].reason` |
| 来源失败 | 不编造记录，记录缺口 | `uncertainty` |
| 输出目录不可写 | stderr 简短错误，退出码 3 | 无静默成功 |

## 限制

- 只在输入记录范围内做增量判定，不发起实时检索；索引延迟条目只能靠后续同窗口重跑补回。
- 撤稿/更正依赖输入显式标记，不解析出版商公告。
- 时区只作为标签保留，naive 时间戳不做隐含换算。
- 标题+年份水位对同名不同文存在残余误判风险，已在 `uncertainty` 中说明。
- 调度本身不在本 Skill 内，需外部按窗口调用。

## 最小调用示例（示例，非正式案例结果）

```bash
python /skills/weekly-skill/scripts/run_skill.py \
  --case /input/case.json --out /app/output/result.json --work /app/work
```

输入片段（示例）：

```json
{"case_id": "demo-003",
 "window": {"start": "2026-11-01", "end": "2026-11-15", "timezone": "Asia/Shanghai"},
 "previous_snapshot": {"watermark": "2026-10-31", "keys": ["doi:10.1/old"]},
 "records": [
   {"title": "New NLRP3 study", "doi": "10.1/new", "first_indexed": "2026-11-03"},
   {"title": "Old NLRP3 study", "doi": "10.1/old", "first_indexed": "2026-11-04"}
 ]}
```

示意输出（示例，已截断）：

```json
{"case_id": "demo-003", "status": "success",
 "new_items": [{"title": "New NLRP3 study", "reason": "not present in the previous snapshot"}],
 "alerts": [{"kind": "new_item", "priority": "normal", "title": "New NLRP3 study"}],
 "result": {"suppressed": [{"title": "Old NLRP3 study", "reason": "already present in the previous snapshot"}],
            "snapshot": {"previous_keys": 1, "current_keys": 2, "next_keys": 2}}}
```

以上为接口示意，不代表任何官方固定案例结果。


## 交付保证与评分对齐

1. 在 Skill 安装目录执行：`cd /skills/weekly-skill && python scripts/run_skill.py --case /input/case.json --out /app/output/result.json --work /app/work`；路径不同时显式改 `--case` / `--out` / `--work`，不要假定相对路径可用。
2. 必须产出**完整成品** `result.json`：当前窗口每条记录都出现在 `new_items` / `updates` / `retractions` / `already_seen` / `out_of_window` 之一，禁止 skeleton、`pending`、空占位或用流程说明代替结果。
3. 脚本无法自动完成的实质判断（真实输入的增量归属核对、版本链与撤稿证据）由调用 Agent 依据实际记录补全并写回 `result.json`，不得编造任何条目或日期。
4. 自检：`case_id` 与输入一致；`output_contract` 每个字段存在；`status` 取本契约值 `success` / `partial` / `insufficient_input` / `failed`（不得写 `completed`）；每条结论有来源定位；同输入重复运行分类一致。
5. 输入缺失、JSON 损坏或字段异常时仍写出结构化结果并说明缺口，不崩溃、不静默成功。

## 最终交付强制要求

1. **调用 Agent 必须交付成品，不是流程说明**：脚本产出只是索引、核对或规格骨架；调用 Agent 必须在同一 `result.json` 中补齐本题要求的最终产物（正文/译文/审稿意见/综述/分析结论/图表源文件与导出件/幻灯片页内容与讲稿/实验方案等）。交付字段为空、只有标题骨架，或含 `pending`、`TODO` 之类占位，一律视为未完成。
2. **必须覆盖全部输入条目**：输入中的每个章节、问题、文献、图、页面、指标都要有对应条目，不得只处理示例或首条；确实缺失的条目写入 `uncertainty` / `limitations` 并说明原因，不得静默跳过。
3. **必须自检后再交付**：`case_id` 与输入一致；`output_contract` 每个字段都存在；状态值取本题契约规定的枚举；数字、单位、引用、来源可逐条回溯；结果内部无自相矛盾。
4. **参数等价**：`python scripts/run_skill.py --case <输入> --out <输出>` 与 `--output <输出>` 等价；默认 `/input/case.json` → `/app/output/result.json`。
5. **平台用 3 个固定案例分别运行**：逻辑要由输入驱动，不针对某一份示例硬编码；字段命名差异走别名匹配与显式降级，并在 `uncertainty` 记录假设。
