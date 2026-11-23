# 证据、契约与幂等回放

## 1. 每条结论都要有定位

结果中的每个分类条目都带 `evidence`，元素形如：

```json
{"file": "crossref.json", "pointer": "/message/items/0", "kind": "record"}
{"file": "europepmc.jsonl", "line": 2, "kind": "record"}
{"file": "records.xml", "xpath": "./records/record[2]", "kind": "record"}
{"file": "records.csv", "row": 3, "kind": "record"}
```

- JSON 用 RFC 6901 JSON Pointer；`~` 与 `/` 分别转义为 `~0`、`~1`。
- JSONL 用 1 起始行号。
- XML 用元素路径，CSV 用 1 起始数据行号。
- 证据只描述"记录来自哪里、为什么这样分类"（`query`、`condition`），不复制大段原文。

## 2. 运行时契约

`/input/case.json` 的 `output_contract` 是唯一权威。脚本对每个键依次尝试：

1. **直接映射**：键名与本 Skill 固定字段同名，直接取真实值；
2. **别名映射**：`records`→`new_items`、`changes`→`updates`、`withdrawn`→`retractions`、`seen`→`already_seen`、`missed`→`out_of_window`、`refs`→`evidence`、`state`→`snapshot`、`notes`→`uncertainty` 等；
3. **尽力而为**：无法对应时给出该键语义上最接近的真实内容（如 `run_summary` 取摘要字符串），并在 `contract_extras` 说明来源；
4. 绝不编造记录来填键。空窗口就是空数组，并在 `uncertainty` 说明。

无论契约如何，`result.json` 顶层始终包含固定字段：`case_id`、`status`、`subscription`、`window`、`new_items`、`updates`、`retractions`、`already_seen`、`out_of_window`、`duplicates`、`alerts`、`suppressed`、`evidence`、`snapshot`、`uncertainty`、`run_log`。

## 3. 幂等回放（交付前必做）

1. 跑第一次，得到 `/app/output/result.json`；
2. 取出其中的 `next_snapshot`；
3. 复制输入目录，把 `case.json` 的 `previous_snapshot` 替换为该 `next_snapshot`；
4. 对副本再跑一次；
5. 期望：`status=success`、`new_items=0`、`updates=0`、`retractions=0`、`alerts=0`，`already_seen` 等于上一轮条目数，快照键数不减少。

若第二轮仍有新增，说明去重键或版本链不稳定，或快照写入不完整，必须修复后再交付。

## 4. 状态判定

| 情况 | status |
|---|---|
| 窗口与快照齐备（即使窗口为空） | `success` |
| 有记录，但缺窗口或快照 | `partial` |
| 无 case、无可用记录、或无法解析 | `insufficient_input` |
| 输出目录不可写等致命错误 | `failed`（退出码非 0） |

`status` 不因"没有新增"而降级；空结果本身是有效结论。
