# 追踪协议：窗口、快照与分类

## 1. 一轮运行做什么

一次运行只回答一个问题：**相对上一次快照，当前窗口内出现了哪些值得提醒的变化。**

顺序固定，避免分类互相污染：

1. 解析订阅与时间窗；
2. 读取上一次快照（键集合 + 记录 + 水位）；
3. 递归收集当前窗口的全部结构化记录；
4. 归一化字段并去重；
5. 合并版本链（预印本 ↔ 正式发表 ↔ 修订）；
6. 与快照差分，得到六个互斥分类；
7. 生成提醒并抑制；
8. 写出 `next_snapshot` 与 `result.json`。

## 2. 时间窗

- 支持 `window` / `time_window` / `period` / `date_range` 及 `window_start` / `window_end` 平铺写法。
- `start`、`end` 均可为 ISO 日期或时间；仅日期的 `end` 扩展到当日 `23:59:59.999999`。
- `timezone` 只做**标签保留与回显**，不擅自换算。naive 时间按原值比较；带时区的时间在可能时转到窗口时区，无法加载时区库时按墙上时间处理，不偏移日期。
- 窗口不完整时不做窗外过滤，并写入 `uncertainty.type=window_incomplete`。

## 3. 六个分类

| 分类 | 判定 |
|---|---|
| `new_items` | 快照中没有，且窗口内首次出现 |
| `updates` | 快照中已有同一实体，本次记录带更高版本、修订或更正标记 |
| `retractions` | 记录带撤稿/撤回标记，或本身就是撤稿声明 |
| `already_seen` | 快照中已有，且无版本或状态变化 |
| `out_of_window` | 实体不在快照中，但日期落在窗口外 |
| `late_indexed` | 出版日期早于窗口开始，索引日期落在窗口内 |

`late_indexed` 默认抑制提醒，但仍保留在结果里供复核；策略见 `assets/policy.json` 的 `window.late_indexed_policy`。

## 4. 日期选择

- 窗口内判定优先使用**索引/入库日期**（`first_indexed`、`entry_date`、`indexed`、`created` 等），缺失时才退回出版日期。
- 记录同时带出版日期与索引日期时，两者都保留在证据里，便于解释"为什么算窗内"。
- 只有年份精度时，不因该年 1 月 1 日早于窗口起点就误判为 `late_indexed`。

## 5. 快照水位

- `previous_snapshot` 可以是键数组、记录数组，或 `{keys, records, watermark}` 结构；兼容 `last_snapshot` / `baseline_snapshot` / `state`。
- 差分先按标识符（DOI → PMID → arXiv）匹配，再按归一化标题 + 年份匹配。
- `next_snapshot.keys` = 旧键 ∪ 当前分类中出现的键；`watermark` 取窗口上界。
- 把 `next_snapshot` 原样填回 `previous_snapshot` 再跑一次，应得到 `new_items=0`、`alerts=0`。

## 6. 幂等

同一输入重复运行必须产生相同的分类与提醒顺序。脚本不读取也不改写历史快照文件，所有状态只来自本次输入。
