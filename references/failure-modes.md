# 失败模式与正确处理

| 情形 | 正确处理 | 结果中的痕迹 |
|---|---|---|
| `/input` 为空 | 仍写出完整结构，空列表 | `status=insufficient_input`；`uncertainty.type=no_case_file`、`no_records_found` |
| `case.json` 损坏 | 捕获解析错误，不崩溃 | `status=insufficient_input`；`errors[].type=JSONDecodeError`；`uncertainty.type=case_parse_error` |
| 窗口为空（无新增） | 输出空 `new_items` / `alerts` | `status=success`；不制造条目 |
| 缺少上一次快照 | 作为基线运行 | `status=partial`；`uncertainty.type=no_previous_snapshot` |
| 窗口起止缺失 | 不做窗外过滤 | `uncertainty.type=window_incomplete` |
| 时区缺失 | 按墙上时间比较，不猜测 | `uncertainty.type=timezone_missing`；`window.timezone=null` |
| 记录缺日期 | 保留记录，不推断 | `uncertainty.type=missing_dates`；该条不入窗外分类 |
| 来源限流 / 抓取不完整 | 只处理已落盘的记录 | `uncertainty` 说明覆盖缺口；`status` 不高于 `partial` |
| 来源改版（字段改名） | 走别名与嵌套探测；仍读不到则记录缺口 | `errors[]` / `uncertainty` 指出文件与字段 |
| 撤稿 / 更正 | 单独成类，优先提醒 | `retractions`；`alerts[].kind=retraction` |
| 重复记录 | 合并，只提醒一次 | `duplicates` 记录重复组与定位 |
| 提醒超预算 | 截断并逐条说明 | `suppressed[].reason=over_max_items` |
| 输出目录不可写 | stderr 报错，非 0 退出 | 不静默成功 |

## 原则

- **先保证结构合法，再谈内容完整**：任何输入都不能让入口崩溃。
- **不确定就降级**：`partial` / `insufficient_input` 比虚假的 `success` 可信。
- **不联网**：运行期没有网络，也不尝试补抓；缺口只能靠后续输入或重跑补回。
- **不自评**：结果里不写"质量良好""结果准确"之类判断。
