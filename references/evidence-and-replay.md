# 证据、契约与幂等回放

## 1. 每条结论都要有定位

结果中的每个分类条目都带 `evidence`：

```json
{"file": "records.csv", "locator": "records.csv#row=3"}
{"file": "crossref.json", "locator": "crossref.json#/message/items/0"}
{"file": "europepmc.jsonl", "locator": "europepmc.jsonl#L2"}
{"file": "snapshot.json", "locator": "snapshot.json#/record_ids/1"}
```

- JSON 用 RFC 6901 JSON Pointer；JSONL 用 1 起始行号；CSV 用 1 起始物理行号；
- 快照本身也有 `source_file` + `locator`；
- 证据只说明"来自哪个文件哪一行"，不复述大段原文。

## 2. 运行时契约

`/input/case.json` 的 `output_contract` 是唯一权威。对每个键：

1. **产物键**（如 `incremental_state_json`、`report_md`、`provenance_json`、
   `records_csv`、`search_ir`）→ 值写对应产物文件名，产物必须真实存在；
2. **固定字段键**（`case_id`、`status`、`summary`）→ 直接给真实值；
3. **别名键**（`records`→`new_items`、`changes`→`updates`、`withdrawn`→`retractions`、
   `refs`→`evidence`、`state`→`next_snapshot`、`notes`→`uncertainty`）→ 映射真实内容；
4. 无法对应时写入 `contract.extras` 说明，不编造记录来填键。

顶层固定字段：`case_id`、`status`、`status_reason`、`summary`、`subscription`、
`window`、`new_items`、`updates`、`retractions`、`already_seen`、`duplicates`、
`alerts`、`suppressed`、`evidence`、`snapshot`、`next_snapshot`、`uncertainty`、
`sources`、`artifacts`、`validation`、`stats`、`contract`。

`artifacts[]` 登记每个产物的 `name`、`role`、`bytes` 与 `sha256`，便于复核。

## 3. 幂等

同一输入重复运行必须逐字节一致（无时钟、无随机、排序确定）。
`next_snapshot` 提供下一轮起点；以同一批快照重跑不会产生二次提醒。

## 4. 状态判定

| 情况 | status |
|---|---|
| 快照与记录齐备（即使窗口为空） | `success` |
| 有记录但快照缺失、ID 未解析或窗口有缺口 | `partial` |
| 无 case、无记录、无法解析 | `insufficient_input` |
| 引擎异常 | `failed`（仍写 result.json，退出码 0） |

`status` 不因"本窗口没有新增"而降级；空结果是有效结论。
