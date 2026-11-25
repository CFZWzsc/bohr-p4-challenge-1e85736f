# Quickstart：离线复现 demo-case

`examples/demo-case/` 是**合成**最小案例（DOI 全为虚构），用于验证快照差分、
版本更新、撤稿、窗口审计与产物契约。它不是官方评分案例，不代表真实检索结果。

## 1. 运行

在 Skill 根目录（含 `scripts/`、`examples/`）执行：

```bash
python scripts/sk_entry.py \
  --input examples/demo-case \
  --work /tmp/lit-demo-work \
  --out /tmp/lit-demo-out
echo "exit=$?"
```

`scripts/run_skill.py` 为等价入口。

## 2. 预期产物

`/tmp/lit-demo-out/` 下应有：

`incremental_state.json`、`report.md`、`provenance.json`、`records.csv`、
`search_ir.json`、`validation.json`、`result.json`。

`result.json` 关键字段（见 `examples/demo-case/expected.json`）：

| 字段 | 期望 |
|---|---|
| `status` | `success` |
| `case_id` | `demo-incremental-tracking-002` |
| `new_items` | 1（SN2 的 R5） |
| `updates` | 1（R3 是 R1 的正式版，同 DOI） |
| `retractions` | 1（R4） |
| `already_seen` | 1（R2 已在 SN1） |
| `alerts` | 3（1 撤稿 + 1 更新 + 1 新增） |
| `stats.window_issues` | 0（SN1 止 = SN2 起） |
| `contract.all_present` | `true` |

基线快照 SN1 的 R1/R2 **不**计入新增，这是本题的关键判定。

## 3. 校验片段

```bash
python - <<'PY'
import json
d = json.load(open("/tmp/lit-demo-out/result.json", encoding="utf-8"))
assert d["status"] == "success"
assert d["case_id"] == "demo-incremental-tracking-002"
assert len(d["new_items"]) == 1
assert len(d["updates"]) == 1 and d["updates"][0]["previous"]["id"] == "R1"
assert len(d["retractions"]) == 1
assert d["stats"]["already_seen"] == 1
assert d["contract"]["all_present"] is True
for name in ("incremental_state.json", "report.md", "provenance.json"):
    assert d["contract"]["mapped"][name.rsplit(".", 1)[0] + "_" +
                                   ("md" if name.endswith(".md") else "json")] == name
print("demo OK")
PY
```

## 4. 幂等回放

以同一批快照重跑，`incremental_state.json`、`report.md` 与 `result.json` 应逐字节一致：

```bash
python scripts/sk_entry.py --input examples/demo-case --work /tmp/w2 --out /tmp/o2
cmp /tmp/lit-demo-out/result.json /tmp/o2/result.json
```

`result.json` 的 `next_snapshot` 给出下一轮起点（水位 + 已见键）；
把它作为新的快照基线再跑，新增与提醒应为 0。

## 5. 空输入与损坏输入

```bash
mkdir -p /tmp/lit-empty
python scripts/sk_entry.py --input /tmp/lit-empty --work /tmp/w3 --out /tmp/o3
echo "exit=$?"   # 0
python -c "import json;d=json.load(open('/tmp/o3/result.json',encoding='utf-8'));print(d['status'],[u['item'] for u in d['uncertainty']])"
# 期望：insufficient_input + case.json / records
```

损坏的 `case.json` 同样退出码 0，`status=insufficient_input`，并在
`uncertainty` 中记录解析缺口。
