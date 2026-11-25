# 追踪协议：快照、窗口与分类

## 1. 一轮运行做什么

一次运行只回答一个问题：**相对上一次快照，当前窗口出现了哪些值得提醒的变化。**

顺序固定：

1. 读 `case.json`（`case_id`、`output_contract`、订阅、窗口）；
2. 发现并解析导入快照（`snapshot_id` / `window` / `record_ids`）；
3. 解析记录表（CSV / Crossref / EuropePMC / DataCite / JSONL / XML）；
4. 归一化字段并按去重键聚类；
5. 以第一个快照为基线，逐窗差分；
6. 分类：新增 / 版本更新 / 撤稿 / 已见；
7. 生成提醒并抑制；
8. 写 `incremental_state.json`、`report.md`、`provenance.json`、`records.csv`、`result.json`。

## 2. 快照与窗口

- 快照来自输入文件（常见 `import_state_*.json`），字段为
  `snapshot_id`、`window.start`、`window.end`、`imported_at`、`record_ids`；
- 按 `window.start` → `imported_at` → `snapshot_id` 排序；
- **第一个快照是基线**：它的记录只进入"已见键"，不计为新增、不推送；
- 若 `case.json` 没有快照文件，则退回 `previous_snapshot` + `window` 的写法；
- `timezone` 只保留与回显，不擅自换算；日期用 `YYYY-MM-DD` 比较。

## 3. 四个分类

| 分类 | 判定 |
|---|---|
| `new` | 该键此前未在任何快照出现过 |
| `version_update` | 该键已出现，但记录 ID 不同，或载体类型/刊名变化，或显式 `version_of` |
| `retraction` | `retracted=true`，或记录标题是撤稿/更正声明 |
| `already_seen` | 同键同实体，且无版本或状态变化 |

撤稿优先于新增：被撤稿记录绝不作为新论文推送。

## 4. 窗口审计

相邻窗口应当首尾相接（`prev.end == cur.start`）：

- 出现未覆盖区间 → `window_issues` 记 `gap`，处置为"待显式回补"，不静默放宽；
- 出现重叠 → 记 `overlap`，处置为"跨窗按去重键单次计数"。

## 5. 水位与回放

- 水位 = 最近窗口的结束日期；同时保留全部已见去重键；
- `next_snapshot` 可直接作为下一轮基线；
- 以同一批快照重跑，增量与提醒完全相同（幂等）。

## 6. 确定性

同一输入重复运行必须逐字节一致：不读时钟、不用随机数、所有列表按固定键排序。
