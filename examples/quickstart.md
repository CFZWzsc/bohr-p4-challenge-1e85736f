# Quickstart：离线复现 demo-case

`examples/demo-case/` 是一个**合成**最小案例，用于验证入口、去重、版本链、撤稿、提醒与幂等。它不是官方评分案例，不代表任何真实检索结果。

## 1. 运行

在 Skill 根目录（含 `scripts/`、`examples/`）执行：

```bash
python scripts/sk_entry.py \
  --input examples/demo-case \
  --work /tmp/lit-demo-work \
  --out /tmp/lit-demo-out
echo "exit=$?"
```

`run_skill.py` 是等价入口：

```bash
python scripts/run_skill.py --input examples/demo-case --work /tmp/lit-demo-work --out /tmp/lit-demo-out
```

## 2. 预期结果

`/tmp/lit-demo-out/result.json` 应满足：

| 字段 | 期望 |
|---|---|
| `status` | `success` |
| `case_id` | `demo-lit-tracking-001` |
| `new_items` | 2（正式论文 C、仅有标题的 D） |
| `updates` | 1（论文 B 的版本 2 修订） |
| `retractions` | 1（论文 A 的撤稿声明） |
| `already_seen` | 0 |
| `out_of_window` | 0 |
| `duplicates` | 1（DOI 大小写/URL 变体合并） |
| `alerts` | 4（1 撤稿 + 1 更新 + 2 新增） |
| `snapshot.advanced` | `true` |
| `contract.all_present` | `true` |
| `run_log` 末项 | `write_output`，无自检失败项 |

同时生成 `/tmp/lit-demo-out/run-review.md`。

## 3. 校验片段

```bash
python - <<'PY'
import json
d = json.load(open("/tmp/lit-demo-out/result.json", encoding="utf-8"))
expected = {"new_items": 2, "updates": 1, "retractions": 1,
            "duplicates": 1, "alerts": 4}
for key, want in expected.items():
    got = len(d[key])
    assert got == want, (key, got, want)
assert d["case_id"] == "demo-lit-tracking-001"
assert d["status"] == "success"
assert d["contract"]["all_present"] is True
assert d["snapshot"]["advanced"] is True
assert all(item.get("evidence") for cat in ("new_items", "updates", "retractions")
           for item in d[cat])
print("demo OK", {k: len(d[k]) for k in expected})
PY
```

## 4. 幂等回放

```bash
mkdir -p /tmp/lit-replay
cp examples/demo-case/*.json examples/demo-case/*.jsonl /tmp/lit-replay/
python - <<'PY'
import json
case = json.load(open("examples/demo-case/case.json", encoding="utf-8"))
res = json.load(open("/tmp/lit-demo-out/result.json", encoding="utf-8"))
case["previous_snapshot"] = res["next_snapshot"]
json.dump(case, open("/tmp/lit-replay/case.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
PY
python scripts/sk_entry.py --input /tmp/lit-replay \
  --work /tmp/lit-replay-work --out /tmp/lit-replay-out

python - <<'PY'
import json
d = json.load(open("/tmp/lit-replay-out/result.json", encoding="utf-8"))
assert d["status"] == "success"
assert len(d["new_items"]) == 0 and len(d["alerts"]) == 0
assert len(d["already_seen"]) == 4
print("replay OK")
PY
```

## 5. 空输入与损坏输入

```bash
mkdir -p /tmp/lit-empty
python scripts/sk_entry.py --input /tmp/lit-empty --work /tmp/lit-empty-work --out /tmp/lit-empty-out
echo "exit=$?"   # 0
python -c "import json;d=json.load(open('/tmp/lit-empty-out/result.json',encoding='utf-8'));print(d['status'],[u['type'] for u in d['uncertainty']])"
# 期望：insufficient_input + no_case_file / no_records_found 等
```

损坏的 `case.json` 同样以退出码 0 结束，`status=insufficient_input`，并在 `errors` 与 `uncertainty` 中记录解析错误。
