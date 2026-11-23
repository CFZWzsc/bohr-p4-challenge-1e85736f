---
name: incremental-literature-alerting
description: 当任务要求对固定时间窗做最新文献发现与增量追踪时使用：解析订阅与窗口、递归读取 Crossref/EuropePMC/DataCite/XML/JSONL/CSV 记录、按 DOI→PMID→arXiv→标题+年份去重、合并预印本/正式版/修订版本链、与上一次快照差分出新文献/版本更新/撤稿/已见/窗外/索引滞后、按规则抑制提醒，并输出可幂等回放且完整覆盖运行时 output_contract 的 result.json 与证据索引。
---

# 最新文献发现与增量追踪

## 1. 用途与适用条件

本 Skill 用于**按时间窗增量跟踪一个文献订阅**：告诉你相对上一次快照，这一窗口里出现了哪些新论文、版本更新、撤稿或更正，以及哪些条目因重复、已见、窗外或索引滞后而不该提醒。

适用条件：

- 输入提供当前窗口的来源记录（Crossref / Europe PMC / DataCite / 通用 REST 导出 / JSONL / CSV / XML 均可）；
- 理想情况下还提供上一次快照（键集合、记录或水位）与窗口起止；
- 需要"本轮相对上轮新增了什么"的可复核结论，而不是一次性主题检索列表。

不适用：全文精读、自动写综述、定时抓取、把提醒条目当作科学结论。本 Skill **不联网**，窗口与来源全部来自输入。

## 2. 输入

| 输入 | 说明 |
|---|---|
| `/input/case.json` | 主描述文件，常见键：`case_id`、`subscription`（含 `query`/`sources`/`alert_rules`）、`window`（`start`/`end`/`timezone`）、`previous_snapshot`（`keys`/`records`/`watermark`）、`output_contract`。键名以实际文件为准。 |
| `/input/task.md` | 可选，用于核对订阅主题、窗口与提醒预算。 |
| 其他来源文件 | 递归扫描 `.json`、`.jsonl`、`.ndjson`、`.xml`、`.txt`、`.csv`、`.tsv`；支持 Crossref `message.items`、Europe PMC `resultList.result`、DataCite `data[].attributes`、OAI/XML 记录节点，以及 JSONL 每行一条记录。 |
| `--input` | 输入目录或 `case.json` 路径，默认 `/input`。 |
| `--work` | 工作目录，默认 `/app/work`。 |
| `--out` | 输出目录或 `*.json` 路径，默认 `/app/output`。 |

识别字段：标题、DOI、PMID、arXiv、作者、期刊、`version`、`first_indexed`/`entry_date`/`indexed`/`created`、`published`/`issued`/`date`、`retracted`/`withdrawn`、`update_type`/`correction`、`relation`/`related_identifiers`。

## 3. 调用方式

在 Skill 根目录执行：

```bash
python scripts/sk_entry.py \
  --input /input \
  --work /app/work \
  --out /app/output
```

`scripts/run_skill.py` 是等价薄封装，参数相同。执行后必须存在：

- `/app/output/result.json` —— 固定字段 + 运行时契约字段齐全的最终结果；
- `/app/output/run-review.md` —— 证据索引与不确定性清单（脚本先写一版，执行 Agent 复核后覆盖）。

## 4. 输出字段说明

`result.json` 顶层始终包含：

| 字段 | 含义 |
|---|---|
| `case_id` | 原样保留输入的 `case_id`；缺失时为空字符串 |
| `status` | `success` / `partial` / `insufficient_input` / `failed` |
| `status_reason` | 状态的可读理由 |
| `summary` | 一句话摘要（新增/更新/撤稿/提醒数） |
| `subscription` | 订阅查询、来源与提醒规则的回显 |
| `window` | `start`/`end`/`timezone`/`complete`/`end_inclusive`/`locator` |
| `new_items` | 快照中没有、窗口内的新条目 |
| `updates` | 同一实体的版本更新/修订/更正 |
| `retractions` | 撤稿声明或被撤稿条目 |
| `already_seen` | 快照中已有且无变化 |
| `out_of_window` | 不在快照中但日期落在窗口外 |
| `late_indexed` | 出版早于窗口、索引落在窗口内（默认不提醒） |
| `duplicates` | 重复组：`dedup_key`、成员定位、来源 |
| `alerts` | 排序后的提醒：`kind`、`priority`、`reason`、证据 |
| `suppressed` | 被抑制的条目与原因 |
| `evidence` | 窗口、快照、来源文件的全局证据 |
| `snapshot` | 上一轮键数、当前键数、下一轮键数、水位、是否推进、记录数 |
| `next_snapshot` | `{watermark, keys, records, window, generated_by, skill_version}`，可直接回填 |
| `uncertainty` | 不确定项：`type`、`detail`、`resolution` |
| `run_log` | 分步执行记录：发现输入、解析 case/窗口、读快照、收集、去重、连链、差分、提醒、写快照、契约、写输出 |
| `stats` | 各分类计数与文件数 |
| `contract` | `declared_keys`、`mapped`、`extras`、`all_present` |
| `contract_extras` | 无法精确映射的契约键说明 |
| `errors` | 解析/读取错误；正常为空 |

`output_contract` 的每个键都会出现在顶层：先直接映射，再走别名（`records`→`new_items`、`changes`→`updates`、`withdrawn`→`retractions`、`seen`→`already_seen`、`missed`→`out_of_window`、`refs`→`evidence`、`state`→`snapshot`、`notes`→`uncertainty` 等），最后才尽力而为并在 `contract_extras` 说明。**绝不为填键编造记录。**

## 5. 去重与版本链规则

1. 去重键优先级：`doi` → `pmid` → `arxiv` → `归一化标题+年份`。
2. 同一键的多条记录合并为一条：保留全部来源定位与全部标识符，日期取最晚索引日期。
3. 版本链通过 `relation`/`related_identifiers`、归一化标题 + 作者交集 + 年份兼容、`version` 递增连接。
4. 链上正式版优先于预印本；所有版本定位都保留，不丢弃预印本来源。
5. 同一实体只提醒一次；版本变化进 `updates`，不重复进 `new_items`。
6. 撤稿声明是独立实体，不与被撤稿论文合并，进 `retractions`。

细则见 `references/dedup-and-versioning.md`。

## 6. 提醒抑制规则

排序：`retraction`（high）→ `version_update`（medium）→ `new_item`（normal）。

以下条目不进入 `alerts`，但保留在结果中并写入 `suppressed` 及原因：

- 重复组成员（`duplicate`）；
- `already_seen`；
- `out_of_window`；
- `late_indexed`（默认策略，可在 `assets/policy.json` 调整）；
- 超过 `alert_rules.max_items` 的条目（默认 25，取输入中的值优先）。

每条提醒带 `kind`、`priority`、`reason` 与证据定位。

## 7. 幂等回放步骤

1. 首次运行，得到 `/app/output/result.json`；
2. 读取其中的 `next_snapshot`；
3. 复制输入目录，把 `case.json` 的 `previous_snapshot` 替换为该 `next_snapshot`；
4. 对副本再次运行；
5. 期望：`status=success`、`new_items=0`、`updates=0`、`retractions=0`、`alerts=0`，`already_seen` 等于上轮条目数，快照键数不减少。

完整命令见 `examples/quickstart.md`。

## 8. 异常处理

| 情形 | 处理 |
|---|---|
| 空窗口（无新增） | 输出空 `new_items`/`alerts`，`status=success`，不制造条目 |
| 限流 / 来源抓取不完整 | 只处理已落盘记录，`status` 不高于 `partial`，`uncertainty` 说明覆盖缺口 |
| 来源改版（字段改名/嵌套变化） | 走别名与嵌套探测；仍读不到则写 `errors` 与 `uncertainty`，指出文件与字段 |
| 日期缺失 | 保留记录，不推断日期；写入 `uncertainty` |
| 时区缺失 | 按墙上时间比较，不猜测；`window.timezone=null` |
| 撤稿/更正 | 单独成类并优先提醒 |
| 无 `case.json` / JSON 损坏 | 退出码 0，`status=insufficient_input`，`errors` + `uncertainty` 记录原因 |
| 输出目录不可写 | stderr 报错，非 0 退出，不静默成功 |

## 9. 交付前自检清单

- [ ] 入口退出码为 0；
- [ ] `result.json` 可解析，含本文件第 4 节全部固定字段；
- [ ] `case_id` 与输入一致；
- [ ] `output_contract` 每个键存在，`contract.all_present=true`；
- [ ] `status` 取 `success`/`partial`/`insufficient_input`/`failed` 之一；
- [ ] 每条 `new_items`/`updates`/`retractions`/`already_seen`/`out_of_window`/`late_indexed` 都有 `evidence`；
- [ ] 计数与 `stats` 一致，无自检失败项；
- [ ] 重复组已合并，撤稿与版本更新分类正确；
- [ ] `snapshot.advanced` 为 `true`（非回放场景）；
- [ ] 回放测试 `new_items=0`、`alerts=0`；
- [ ] 空/损坏输入退出码 0 且状态诚实。

## 10. 执行 Agent 的职责（必读）

脚本只做确定性的读取、归一化、去重、差分与写盘；**它不判断"这条记录是否真的属于该订阅主题"**，也不负责最终交付说明。执行 Agent 必须：

1. 运行入口，得到 `/app/output/result.json`；
2. 逐类读取 `new_items`、`updates`、`retractions`、`already_seen`、`out_of_window`、`late_indexed`、`duplicates`、`alerts`、`suppressed`；
3. 对照每条目的 `evidence`（文件 + JSON Pointer / 行号 / XPath / CSV 行）**逐条回到源文件核对**标题、DOI、日期与撤稿/版本标记；
4. 复核计数与 `stats`、`snapshot` 是否自洽；发现不一致时修正输入分析或代码，不得手改结果掩盖；
5. 用核对后的事实覆盖 `/app/output/run-review.md`，写清：运行命令、各分类计数、逐条证据定位、未解决问题；
6. **不编造**：源文件没有的条目、日期、撤稿标记一律不写；拿不准的写入 `uncertainty` 并在复核文件中标注；
7. 不提交、不上传、不写入任何 token。

## 11. 参考

- `references/tracking-protocol.md` —— 窗口、快照、六分类与水位
- `references/dedup-and-versioning.md` —— 去重键与版本链
- `references/evidence-and-replay.md` —— 证据定位、契约映射、幂等回放
- `references/failure-modes.md` —— 失败模式与退路
- `assets/policy.json` —— 去重优先级、提醒预算、窗口规则与限制
- `examples/quickstart.md` —— 可离线复现的 demo 与校验片段
