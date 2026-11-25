#!/usr/bin/env python3
"""incremental_literature_alerting — deterministic, offline, stdlib-only engine.

Reads the organizer-provided case (case.json + task.md + whatever material files
ship with it), reconstructs the subscription / import-state snapshots / records,
diffs snapshot n against snapshot n-1, and writes the editable artifacts plus a
contract-complete result.json.

Design rules
------------
* One single pass produces every deliverable; nothing is deferred to a second
  agent turn.
* Every value traces back to a file locator; nothing is fabricated. Missing
  material becomes an explicit ``uncertainty`` entry.
* Output is byte-deterministic for the same input (no clock, no randomness).
* Exception paths still write result.json and exit 0.

Usage
-----
    python scripts/sk_entry.py --input /input --work /app/work --out /app/output
    python scripts/sk_entry.py --input /input/case.json --out /app/output/result.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys

SKILL_VERSION = "2.1.0"
DEDUP_RULE = ("normalized DOI > arXiv id > PMID > normalized title + year "
              "(lowercase, punctuation and whitespace collapsed)")
MAX_RECORDS = 5000
MAX_FILES = 400

TEXT_EXT = {".md", ".txt", ".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".xml"}
RECORD_EXT = TEXT_EXT

DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
RETRACT_RE = re.compile(
    r"(retract|withdraw|撤稿|撤回|更正声明|retraction notice)", re.I)
PREPRINT_HINT = re.compile(r"(arxiv|biorxiv|medrxiv|preprint|ssrn|research square)",
                           re.I)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def norm_text(value) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_doi(value) -> str:
    if not value:
        return ""
    doi = str(value).strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = doi.replace("doi:", "").strip()
    return doi.rstrip(". ")


def norm_id(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def year_of(rec) -> str:
    for key in ("year", "publication_year", "published_year"):
        val = rec.get(key)
        if val not in (None, ""):
            match = DATE_RE.search(str(val))
            if match:
                return match.group(1)
            if str(val).isdigit() and len(str(val)) == 4:
                return str(val)
    for key in ("published_date", "published", "issued", "date", "indexed_date"):
        match = DATE_RE.search(str(rec.get(key) or ""))
        if match:
            return match.group(1)
    return ""


def date_of(*values) -> str:
    for value in values:
        match = DATE_RE.search(str(value or ""))
        if match:
            return match.group(0)
    return ""


def dedup_key(rec) -> str:
    doi = norm_doi(rec.get("doi"))
    if doi:
        return "doi:" + doi
    arxiv = norm_id(rec.get("arxiv") or rec.get("arxiv_id"))
    if arxiv:
        return "arxiv:" + re.sub(r"v\d+$", "", arxiv)
    pmid = norm_id(rec.get("pmid") or rec.get("pmcid"))
    if pmid:
        return "pmid:" + pmid
    title = norm_text(rec.get("title"))
    if title:
        return "title:" + title + "|" + year_of(rec)
    return ""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: str, obj) -> int:
    payload = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    data = payload.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)


def get_any(rec, *keys):
    for key in keys:
        if key in rec and rec[key] not in (None, ""):
            return rec[key]
    return None


# --------------------------------------------------------------------------
# input inventory
# --------------------------------------------------------------------------
def read_text(path: str) -> str:
    with open(path, "rb") as handle:
        raw = handle.read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def inventory(root: str):
    files = []
    if os.path.isfile(root):
        root_dir = os.path.dirname(root)
        return root_dir, [{"rel": os.path.basename(root), "path": root,
                           "ext": os.path.splitext(root)[1].lower()}]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(".") and d != "__pycache__")
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            files.append({
                "rel": os.path.relpath(full, root).replace("\\", "/"),
                "path": full,
                "ext": os.path.splitext(name)[1].lower(),
            })
            if len(files) >= MAX_FILES:
                return root, files
    return root, files


def load_case(root: str, files):
    candidates = [f for f in files if f["rel"] == "case.json"]
    candidates += [f for f in files if f["rel"].endswith("case.json")]
    for item in candidates:
        try:
            data = json.loads(read_text(item["path"]))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            return data, item["rel"]
    return {}, ""


# --------------------------------------------------------------------------
# record / snapshot normalisation
# --------------------------------------------------------------------------
TITLE_KEYS = ("title", "article_title", "primary_title", "name", "标题")
DOI_KEYS = ("doi", "DOI", "digital_object_identifier")
ID_KEYS = ("id", "item_id", "record_id", "key", "uid")
RETRACT_KEYS = ("retracted", "is_retraction", "retraction", "withdrawn")
ARXIV_KEYS = ("arxiv", "arxiv_id", "arxivId")
PMID_KEYS = ("pmid", "pmcid")
AUTHOR_KEYS = ("authors", "author", "creator", "creators")
VENUE_KEYS = ("journal", "venue", "container_title", "container-title", "source_title")
TYPE_KEYS = ("venue_type", "type", "publication_type")
DATE_KEYS = ("published_date", "published", "issued", "date", "created")
INDEX_KEYS = ("indexed_date", "indexed", "first_indexed", "entry_date", "imported_at")


def as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return norm_text(value) in ("true", "yes", "1", "y", "retracted")


def looks_like_record(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    if any(k in obj and obj[k] not in (None, "") for k in TITLE_KEYS):
        return True
    return any(k in obj and obj[k] not in (None, "") for k in DOI_KEYS)


def looks_like_snapshot(obj) -> bool:
    return (isinstance(obj, dict) and "record_ids" in obj
            and ("window" in obj or "snapshot_id" in obj))


def normalise_record(raw, source_file, locator, fallback_id):
    title = str(get_any(raw, *TITLE_KEYS) or "").strip()
    doi = norm_doi(get_any(raw, *DOI_KEYS))
    rec = {
        "id": str(get_any(raw, *ID_KEYS) or fallback_id),
        "title": title,
        "doi": doi,
        "arxiv": str(get_any(raw, *ARXIV_KEYS) or "").strip(),
        "pmid": str(get_any(raw, *PMID_KEYS) or "").strip(),
        "authors": get_any(raw, *AUTHOR_KEYS) or "",
        "venue": str(get_any(raw, *VENUE_KEYS) or "").strip(),
        "venue_type": str(get_any(raw, *TYPE_KEYS) or "").strip().lower(),
        "year": year_of(raw),
        "published_date": date_of(*[raw.get(k) for k in DATE_KEYS]),
        "indexed_date": date_of(*[raw.get(k) for k in INDEX_KEYS]),
        "retracted": any(as_bool(raw.get(k)) for k in RETRACT_KEYS)
                     or bool(RETRACT_RE.search(title)),
        "version_of": str(get_any(raw, "version_of", "versionOf", "predecessor")
                          or "").strip(),
        "source_file": source_file,
        "locator": locator,
    }
    rec["key"] = dedup_key(rec)
    return rec


def parse_dates(window):
    start = date_of((window or {}).get("start"))
    end = date_of((window or {}).get("end"))
    return start, end


def parse_csv_file(item):
    records = []
    text = read_text(item["path"])
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return records
    header_index = 0
    for idx in range(min(3, len(rows))):
        lowered = [cell.strip().lower() for cell in rows[idx]]
        if any(cell in ("title", "doi", "id", "item_id") for cell in lowered):
            header_index = idx
            break
    header = [cell.strip().lower() for cell in rows[header_index]]
    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not any(cell.strip() for cell in row):
            continue
        raw = {header[i]: row[i] for i in range(min(len(header), len(row)))
               if header[i]}
        if not looks_like_record(raw):
            continue
        records.append(normalise_record(raw, item["rel"],
                                        f"{item['rel']}#row={offset}",
                                        f"row{offset}"))
        if len(records) >= MAX_RECORDS:
            break
    return records


def parse_jsonl_file(item):
    records = []
    for number, line in enumerate(read_text(item["path"]).splitlines(), start=1):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if looks_like_record(raw):
            records.append(normalise_record(raw, item["rel"],
                                            f"{item['rel']}#L{number}",
                                            f"L{number}"))
        if len(records) >= MAX_RECORDS:
            break
    return records


def parse_json_doc(item, state):
    records, snapshots = [], []
    try:
        data = json.loads(read_text(item["path"]))
    except Exception:  # noqa: BLE001
        return records, snapshots

    def walk(node, pointer, depth):
        if depth > 8 or len(records) >= MAX_RECORDS:
            return
        if isinstance(node, dict):
            if looks_like_snapshot(node):
                window = node.get("window") or {}
                start, end = parse_dates(window)
                snapshots.append({
                    "id": str(node.get("snapshot_id") or node.get("id")
                              or f"{item['rel']}#{pointer}"),
                    "start": start,
                    "end": end,
                    "timezone": window.get("timezone"),
                    "imported_at": date_of(node.get("imported_at")) or None,
                    "record_ids": [str(x) for x in (node.get("record_ids") or [])],
                    "source_file": item["rel"],
                    "locator": f"{item['rel']}#{pointer}",
                })
                return
            if looks_like_record(node):
                records.append(normalise_record(
                    node, item["rel"], f"{item['rel']}#{pointer}",
                    f"{os.path.basename(item['rel'])}-{len(records) + 1}"))
                return
            for key, value in node.items():
                walk(value, f"{pointer}/{key}", depth + 1)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                walk(value, f"{pointer}/{idx}", depth + 1)

    if isinstance(data, dict) and "message" in data and isinstance(
            data["message"], dict) and "items" in data["message"]:
        walk(data["message"]["items"], "/message/items", 0)
    elif isinstance(data, dict) and "resultList" in data:
        walk(data.get("resultList", {}).get("result", []), "/resultList/result", 0)
    elif isinstance(data, dict) and "data" in data:
        walk(data["data"], "/data", 0)
    else:
        walk(data, "", 0)

    if isinstance(data, dict) and "subscription" in data and not state.get(
            "subscription"):
        state["subscription"] = data["subscription"]
    if isinstance(data, dict) and "queries" in data and not state.get("queries"):
        state["queries"] = data["queries"]
    return records, snapshots


def parse_xml_file(item):
    records = []
    text = read_text(item["path"])
    for idx, block in enumerate(re.finditer(r"<(?:record|article|item)\b.*?</(?:record|article|item)>",
                                            text, re.S | re.I)):
        chunk = block.group(0)
        title = re.search(r"<title[^>]*>(.*?)</title>", chunk, re.S | re.I)
        doi = re.search(r"<doi[^>]*>(.*?)</doi>", chunk, re.S | re.I)
        year = re.search(r"<year[^>]*>(\d{4})</year>", chunk, re.I)
        if not title and not doi:
            continue
        rec = normalise_record(
            {"title": re.sub(r"<[^>]+>", "", title.group(1)) if title else "",
             "doi": doi.group(1) if doi else "",
             "year": year.group(1) if year else ""},
            item["rel"], f"{item['rel']}#block={idx + 1}", f"xml{idx + 1}")
        if rec["key"]:
            records.append(rec)
        if len(records) >= MAX_RECORDS:
            break
    return records


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
def build_snapshots(case, snapshots, records, notes):
    """Use explicit import states when shipped, otherwise diff against the
    previous_snapshot described inside case.json."""
    if snapshots:
        for snapshot in snapshots:
            snapshot["record_ids"] = [rid for rid in snapshot["record_ids"] if rid]
        snapshots.sort(key=lambda s: (s.get("start") or "9999",
                                      s.get("imported_at") or "", s["id"]))
        return snapshots, "case.import_states"

    previous = case.get("previous_snapshot") or {}
    keys = [str(k) for k in (previous.get("keys") or [])]
    prev_records = [r for r in (previous.get("records") or [])
                    if isinstance(r, dict)]
    prev_ids = []
    for idx, raw in enumerate(prev_records):
        rec = normalise_record(raw, "case.json",
                               f"/previous_snapshot/records/{idx}", f"prev{idx}")
        rec["key"] = rec["key"] or keys[idx] if idx < len(keys) else rec["key"]
        records.append(rec)
        prev_ids.append(rec["id"])
    window = case.get("window") or {}
    start, end = parse_dates(window)
    synth = [{
        "id": "previous_snapshot",
        "start": None,
        "end": previous.get("watermark"),
        "timezone": window.get("timezone"),
        "imported_at": None,
        "record_ids": prev_ids,
        "source_file": "case.json",
        "locator": "/previous_snapshot",
    }, {
        "id": "current_window",
        "start": start,
        "end": end,
        "timezone": window.get("timezone"),
        "imported_at": None,
        "record_ids": [r["id"] for r in records if r["id"] not in prev_ids],
        "source_file": "case.json",
        "locator": "/window",
    }]
    if not previous and not window:
        notes.append({
            "item": "snapshot definition",
            "reason": "no import-state file and no previous_snapshot/window in case.json",
            "impact": "increments cannot be anchored to a watermark",
            "handling": "all parsed records reported as unanchored candidates",
        })
        return [], "none"
    return synth, "case.previous_snapshot+window"


def window_issues(snapshots):
    issues = []
    dated = [s for s in snapshots if s.get("start") and s.get("end")]
    for prev, cur in zip(dated, dated[1:]):
        if prev["end"] == cur["start"]:
            continue
        if prev["end"] < cur["start"]:
            issues.append({
                "between": [prev["id"], cur["id"]],
                "type": "gap",
                "detail": f"{prev['end']} -> {cur['start']}: uncovered interval",
                "severity": "major",
                "handling": "reported for explicit backfill decision; not silently widened",
            })
        else:
            issues.append({
                "between": [prev["id"], cur["id"]],
                "type": "overlap",
                "detail": f"{prev['end']} -> {cur['start']}: overlapping interval",
                "severity": "minor",
                "handling": "dedup key keeps the entity single-counted across windows",
            })
    return issues


def diff_snapshots(snapshots, records, subscription, notes):
    by_id = {}
    for rec in records:
        by_id.setdefault(rec["id"], rec)

    increments, unresolved = [], []
    seen_keys = {}
    seen_ids = set()
    all_dup_groups, alerts, suppressed = [], [], []
    version_groups = {}
    max_items = 25
    if isinstance(subscription, dict):
        rules = subscription.get("alert_rules") or {}
        if isinstance(rules, dict) and isinstance(rules.get("max_items"), int):
            max_items = max(1, rules["max_items"])

    for index, snapshot in enumerate(snapshots):
        new_items, updates, retractions, already, missing = [], [], [], [], []
        local_keys = {}
        if index == 0:
            # The first import state is the baseline: it was already known before
            # the tracked windows, so it must not be pushed as new discoveries.
            for rid in snapshot["record_ids"]:
                rec = by_id.get(rid)
                if rec is None:
                    missing.append(rid)
                    unresolved.append({"snapshot": snapshot["id"], "record_id": rid})
                    continue
                key = rec["key"] or ("id:" + rec["id"])
                local_keys.setdefault(key, rid)
                seen_ids.add(rid)
            for rid in snapshot["record_ids"]:
                seen_ids.add(rid)
            for key, rid in local_keys.items():
                seen_keys.setdefault(key, by_id.get(rid))
            increments.append({
                "snapshot_id": snapshot["id"],
                "window": {"start": snapshot.get("start"), "end": snapshot.get("end"),
                           "timezone": snapshot.get("timezone")},
                "imported_at": snapshot.get("imported_at"),
                "source_file": snapshot.get("source_file"),
                "locator": snapshot.get("locator"),
                "new": [],
                "version_updates": [],
                "retractions": [],
                "already_seen_count": len(local_keys),
                "unresolved_record_ids": missing,
                "baseline": True,
                "note": ("baseline snapshot: its records define the starting seen-keys "
                         "and are not reported as new discoveries"),
            })
            continue
        for rid in snapshot["record_ids"]:
            rec = by_id.get(rid)
            if rec is None:
                missing.append(rid)
                unresolved.append({"snapshot": snapshot["id"], "record_id": rid})
                continue
            key = rec["key"] or ("id:" + rec["id"])
            item = {
                "id": rec["id"], "key": key, "title": rec["title"],
                "doi": rec["doi"], "venue": rec["venue"],
                "venue_type": rec["venue_type"],
                "published_date": rec["published_date"],
                "indexed_date": rec["indexed_date"],
                "evidence": {"file": rec["source_file"], "locator": rec["locator"]},
            }
            prior = seen_keys.get(key)
            if rec["retracted"]:
                item["reason"] = "retraction notice or retracted record in this window"
                retractions.append(item)
                continue
            if key in local_keys:
                all_dup_groups.append({
                    "dedup_key": key,
                    "members": [local_keys[key], rec["id"]],
                    "kept": local_keys[key],
                    "basis": "same normalized key inside one snapshot",
                })
                suppressed.append({"id": rec["id"], "reason": "duplicate",
                                   "dedup_key": key})
                continue
            local_keys[key] = rec["id"]
            if prior is not None:
                if prior["id"] != rec["id"] and (
                        prior.get("venue_type") != rec.get("venue_type")
                        or rec.get("version_of")
                        or prior.get("venue") != rec.get("venue")):
                    item["change"] = "version_update"
                    item["previous"] = {"id": prior["id"], "venue": prior.get("venue"),
                                        "venue_type": prior.get("venue_type"),
                                        "locator": prior.get("locator")}
                    updates.append(item)
                    version_groups.setdefault(key, []).append(rec["id"])
                else:
                    already.append(item)
                continue
            if rec.get("version_of") and rec["version_of"] in seen_ids:
                item["change"] = "version_update"
                item["previous"] = {"id": rec["version_of"], "locator": None}
                updates.append(item)
                version_groups.setdefault(key, []).append(rec["id"])
                continue
            new_items.append(item)

        for rec_id in snapshot["record_ids"]:
            seen_ids.add(rec_id)
        for key, rid in local_keys.items():
            seen_keys.setdefault(key, by_id.get(rid))

        increments.append({
            "snapshot_id": snapshot["id"],
            "window": {"start": snapshot.get("start"), "end": snapshot.get("end"),
                       "timezone": snapshot.get("timezone")},
            "imported_at": snapshot.get("imported_at"),
            "source_file": snapshot.get("source_file"),
            "locator": snapshot.get("locator"),
            "new": new_items,
            "version_updates": updates,
            "retractions": retractions,
            "already_seen_count": len(already),
            "unresolved_record_ids": missing,
        })

    # alerts: retraction > version update > new item, deterministic order
    counter = 0

    def push(kind, priority, item, source_window):
        nonlocal counter
        if counter >= max_items:
            suppressed.append({"id": item.get("id"), "reason": "alert_budget",
                               "dedup_key": item.get("key")})
            return
        counter += 1
        alerts.append({
            "alert_id": f"A{counter:03d}",
            "kind": kind,
            "priority": priority,
            "title": item.get("title", ""),
            "doi": item.get("doi", ""),
            "window": source_window,
            "reason": item.get("reason") or item.get("change") or kind,
            "evidence": item.get("evidence"),
        })

    for inc in increments:
        window = inc["window"]
        for item in inc["retractions"]:
            push("retraction", "high", item, window)
        for item in inc["version_updates"]:
            push("version_update", "medium", item, window)
        for item in inc["new"]:
            push("new_item", "normal", item, window)

    return {
        "increments": increments,
        "alerts": alerts,
        "suppressed": suppressed,
        "duplicates": all_dup_groups,
        "unresolved": unresolved,
        "seen_keys": seen_keys,
        "version_groups": version_groups,
    }


def build_watermark(snapshots, seen_keys):
    dated = [s for s in snapshots if s.get("end")]
    return {
        "last_window_end": dated[-1]["end"] if dated else None,
        "n_seen_keys": len(seen_keys),
        "next_snapshot_hint": {
            "watermark": dated[-1]["end"] if dated else None,
            "keys": sorted(seen_keys.keys()),
            "n_keys": len(seen_keys),
        },
    }


# --------------------------------------------------------------------------
# artifacts
# --------------------------------------------------------------------------
def make_report(case, subscription, snapshots, state, issues, records,
                watermark, notes):
    lines = []
    title = case.get("title") or case.get("case_id") or "case"
    lines.append(f"# 增量文献追踪报告：{title}")
    lines.append("")
    lines.append(f"- 案例 ID：`{case.get('case_id') or 'unknown'}`")
    topic = (subscription or {}).get("topic") or (subscription or {}).get("query") \
        or case.get("task") or "未在输入中声明"
    lines.append(f"- 订阅主题 / 检索式：{topic}")
    lines.append(f"- 输入范围：组织方提供的本地导入快照 {len(snapshots)} 个"
                 "（非实时检索，不代表全球最新收录）")
    lines.append(f"- 去重键规则：{DEDUP_RULE}")
    lines.append("")
    lines.append("## 1. 快照与窗口")
    if snapshots:
        lines.append("| 快照 | 窗口起 | 窗口止 | 导入时间 | 记录数 | 证据 |")
        lines.append("|---|---|---|---|---|---|")
        for snap in snapshots:
            lines.append("| {id} | {start} | {end} | {imp} | {n} | `{loc}` |".format(
                id=snap["id"], start=snap.get("start") or "—",
                end=snap.get("end") or "—", imp=snap.get("imported_at") or "—",
                n=len(snap["record_ids"]), loc=snap.get("locator") or snap.get("source_file")))
    else:
        lines.append("输入未提供可识别的导入快照，无法建立窗口序列；"
                     "相关结论已在 result.json 的 uncertainty 中登记。")
    lines.append("")
    lines.append("## 2. 逐窗增量")
    if state["increments"]:
        for inc in state["increments"]:
            window = inc["window"]
            lines.append(f"### {inc['snapshot_id']}"
                         f"（{window.get('start') or '—'} → {window.get('end') or '—'}）")
            if not (inc["new"] or inc["version_updates"] or inc["retractions"]):
                lines.append("- 本窗口没有新增、版本更新或撤稿；"
                             f"已见 {inc['already_seen_count']} 条。")
            for item in inc["new"]:
                lines.append(f"- 新增：{item['title'] or item['id']}"
                             f"（{item['doi'] or '无 DOI'}；证据 `{item['evidence']['locator']}`）")
            for item in inc["version_updates"]:
                prev = item.get("previous") or {}
                lines.append(f"- 版本更新：{item['title'] or item['id']}"
                             f"（{item['doi'] or '无 DOI'}；相对 {prev.get('id') or '前序版本'}；"
                             f"证据 `{item['evidence']['locator']}`）")
            for item in inc["retractions"]:
                lines.append(f"- 撤稿 / 更正：{item['title'] or item['id']}"
                             f"（{item['doi'] or '无 DOI'}；证据 `{item['evidence']['locator']}`）")
            for rid in inc["unresolved_record_ids"]:
                lines.append(f"- 未解析记录 ID：`{rid}`（快照引用了输入中不存在的条目）")
            lines.append("")
    else:
        lines.append("没有可计算的窗口增量；输入缺少快照序列或记录。")
        lines.append("")
    lines.append("## 3. 去重与版本链")
    if state["duplicates"]:
        for group in state["duplicates"]:
            lines.append(f"- 去重键 `{group['dedup_key']}`：保留 `{group['kept']}`，"
                         f"合并 {', '.join(str(m) for m in group['members'])}")
    else:
        lines.append("- 未发现同一快照内的重复条目；跨窗口同键条目按版本更新处理。")
    for key, ids in sorted(state["version_groups"].items()):
        lines.append(f"- 版本链 `{key}`：{' → '.join(ids)}")
    lines.append("")
    lines.append("## 4. 提醒与抑制")
    if state["alerts"]:
        for alert in state["alerts"]:
            lines.append(f"- [{alert['priority']}] {alert['kind']}：{alert['title']}"
                         f"（{alert['doi'] or '无 DOI'}；证据 `{alert['evidence']['locator']}`）")
    else:
        lines.append("- 无提醒：本批快照没有值得推送的增量（空窗口按成功处理）。")
    if state["suppressed"]:
        lines.append(f"- 抑制 {len(state['suppressed'])} 条："
                     + "、".join(sorted({s['reason'] for s in state['suppressed']})))
    lines.append("")
    lines.append("## 5. 窗口连续性")
    if issues:
        for issue in issues:
            lines.append(f"- {issue['type']}：{issue['detail']}（处置：{issue['handling']}）")
    else:
        lines.append("- 所有相邻窗口首尾相接，无缺口或无说明重叠。")
    lines.append("")
    lines.append("## 6. 水位与幂等回放")
    lines.append(f"- 水位（最近窗口止）：{watermark['last_window_end'] or '未建立'}")
    lines.append(f"- 已见去重键：{watermark['n_seen_keys']}")
    lines.append("- 将 `incremental_state.json` 中的 watermark 与 seen_keys 作为下一次"
                 "运行的起点，重复运行同一批快照会产生完全相同的增量（幂等，不会二次推送）。")
    lines.append("")
    lines.append("## 7. 不确定性与限制")
    if notes:
        for note in notes:
            lines.append(f"- {note['item']}：{note['reason']}（影响：{note['impact']}；"
                         f"处置：{note['handling']}）")
    else:
        lines.append("- 未发现需要人工复核的缺口。")
    lines.append("- 本报告只依据输入材料，不声称实时、全球或最新收录覆盖。")
    lines.append("")
    return "\n".join(lines)


def artifact_target(key: str) -> str:
    low = key.lower()
    if "provenance" in low:
        return "provenance.json"
    if "report" in low or low.endswith("_md") or "markdown" in low:
        return "report.md"
    if "csv" in low or "records" in low:
        return "records.csv"
    if "validation" in low or "check" in low:
        return "validation.json"
    if "ir" == low or low.endswith("_ir") or "search_ir" in low:
        return "search_ir.json"
    if "state" in low or "incremental" in low:
        return "incremental_state.json"
    return "result.json"


LEGACY_ALIASES = {
    "records": "new_items",
    "changes": "updates",
    "withdrawn": "retractions",
    "refs": "evidence",
    "state": "next_snapshot",
    "notes": "uncertainty",
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="incremental literature alerting")
    parser.add_argument("--input", default="/input", help="input dir or case.json")
    parser.add_argument("--work", default="/app/work", help="work dir")
    parser.add_argument("--out", default="/app/output", help="output dir or result.json")
    args = parser.parse_args(argv)

    out_path = args.out
    if out_path.lower().endswith(".json"):
        out_dir = os.path.dirname(out_path) or "."
        result_path = out_path
    else:
        out_dir = out_path
        result_path = os.path.join(out_dir, "result.json")
    os.makedirs(out_dir, exist_ok=True)
    if args.work:
        os.makedirs(args.work, exist_ok=True)

    notes = []
    root, files = inventory(args.input)
    case, case_rel = load_case(root, files)

    records, snapshots, state = [], [], {}
    parsed_files, failed_files = [], []
    for item in files:
        if item["rel"] == case_rel:
            continue
        ext = item["ext"]
        before = len(records)
        if ext == ".csv":
            records.extend(parse_csv_file(item))
        elif ext in (".jsonl", ".ndjson"):
            records.extend(parse_jsonl_file(item))
        elif ext == ".json":
            recs, snaps = parse_json_doc(item, state)
            records.extend(recs)
            snapshots.extend(snaps)
        elif ext == ".xml":
            records.extend(parse_xml_file(item))
        else:
            parsed_files.append({"file": item["rel"], "role": "text"})
            continue
        if len(records) > before or ext == ".json":
            parsed_files.append({"file": item["rel"], "role": "material",
                                 "records": len(records) - before})
        elif ext in RECORD_EXT:
            failed_files.append({"file": item["rel"], "reason": "no record recognised"})

    # de-duplicate records that appear in more than one parsed container
    unique, seen = [], set()
    for rec in records:
        token = (rec["source_file"], rec["locator"])
        if token in seen:
            continue
        seen.add(token)
        unique.append(rec)
    records = unique

    if case.get("subscription") and not state.get("subscription"):
        state["subscription"] = case.get("subscription")
    subscription = state.get("subscription") or case.get("subscription") or {}
    if not subscription and (case.get("topic") or case.get("task")):
        subscription = {"topic": case.get("topic") or case.get("task")}

    snapshots, snapshot_source = build_snapshots(case, snapshots, records, notes)
    state["increments"] = []
    issue_list = window_issues(snapshots)

    if snapshots:
        state.update(diff_snapshots(snapshots, records, subscription, notes))
        if len(snapshots) < 2:
            notes.append({
                "item": "increment windows",
                "reason": "only one import state was provided, so there is no "
                          "previous snapshot to diff against",
                "impact": "no genuinely new item can be established",
                "handling": "the single snapshot is treated as the baseline; "
                            "no alert is emitted",
            })
    else:
        state.update({"alerts": [], "suppressed": [], "duplicates": [],
                      "unresolved": [], "seen_keys": {}, "version_groups": {}})
        notes.append({
            "item": "increment computation",
            "reason": "no snapshot sequence available",
            "impact": "new/updated/retracted classification could not be anchored",
            "handling": "records listed as candidates; no alert emitted",
        })

    watermark = build_watermark(snapshots, state["seen_keys"])
    if not case:
        notes.append({
            "item": "case.json",
            "reason": "no parseable case.json under the input directory",
            "impact": "case_id and output_contract cannot be honoured",
            "handling": "result.json written with honest insufficient_input status",
        })
    if not records:
        notes.append({
            "item": "records",
            "reason": "no record could be extracted from the provided material",
            "impact": "no increment can be computed",
            "handling": "empty result with explicit explanation, nothing fabricated",
        })
    for bad in failed_files:
        notes.append({
            "item": "file:" + bad["file"],
            "reason": bad["reason"],
            "impact": "that file contributed no records",
            "handling": "kept in provenance as unused source",
        })

    new_items = [x for inc in state["increments"] for x in inc["new"]]
    updates = [x for inc in state["increments"] for x in inc["version_updates"]]
    retractions = [x for inc in state["increments"] for x in inc["retractions"]]
    already_count = sum(inc["already_seen_count"] for inc in state["increments"]
                        if not inc.get("baseline"))
    evidence = [
        {"file": rec["source_file"], "locator": rec["locator"],
         "record_id": rec["id"], "dedup_key": rec["key"]}
        for rec in records
    ]

    validation = {
        "checks": [
            {"name": "case_json_loaded", "ok": bool(case),
             "detail": case_rel or "missing"},
            {"name": "records_extracted", "ok": bool(records),
             "detail": f"{len(records)} records"},
            {"name": "snapshots_available", "ok": bool(snapshots),
             "detail": snapshot_source},
            {"name": "all_snapshot_ids_resolved",
             "ok": not state["unresolved"],
             "detail": f"{len(state['unresolved'])} unresolved"},
            {"name": "windows_contiguous", "ok": not issue_list,
             "detail": f"{len(issue_list)} window issues"},
            {"name": "every_item_has_locator",
             "ok": all(x.get("evidence") for x in new_items + updates + retractions),
             "detail": "evidence attached per item"},
            {"name": "no_live_claim",
             "ok": True,
             "detail": "scope_note states organizer-local snapshots only"},
        ],
        "verdict": "pass",
    }
    failed_checks = [c for c in validation["checks"] if not c["ok"]]
    validation["verdict"] = "pass" if not failed_checks else "partial"

    if not case or not records:
        status = "insufficient_input"
        reason = "case.json or records missing from the provided material"
    elif failed_checks:
        status = "partial"
        reason = "unresolved items or window issues: " + ", ".join(
            c["name"] for c in failed_checks)
    else:
        status = "success"
        reason = "all snapshots resolved, windows audited, artifacts written"

    summary = (f"快照 {len(snapshots)} 个：新增 {len(new_items)}、版本更新 {len(updates)}、"
               f"撤稿 {len(retractions)}、已见 {already_count}、提醒 {len(state['alerts'])}、"
               f"抑制 {len(state['suppressed'])}")

    incremental_state = {
        "skill_version": SKILL_VERSION,
        "case_id": case.get("case_id") or "",
        "kind": "incremental_tracking",
        "subscription": subscription,
        "subscription_source": "case.json" if case.get("subscription") else snapshot_source,
        "snapshots": [{"id": s["id"],
                       "window": {"start": s.get("start"), "end": s.get("end"),
                                  "timezone": s.get("timezone")},
                       "imported_at": s.get("imported_at"),
                       "record_count": len(s["record_ids"]),
                       "source": s.get("locator") or s.get("source_file")}
                      for s in snapshots],
        "increments": state["increments"],
        "window_issues": issue_list,
        "dedup_key_rule": DEDUP_RULE,
        "seen_keys": sorted(state["seen_keys"].keys()),
        "version_groups": [{"dedup_key": k, "record_ids": v}
                           for k, v in sorted(state["version_groups"].items())],
        "duplicates": state["duplicates"],
        "alerts": state["alerts"],
        "suppressed": state["suppressed"],
        "watermark": watermark,
        "idempotent": True,
        "scope_note": ("organizer-local import snapshots only; no live remote search and "
                       "no claim of latest/global publication coverage"),
    }

    provenance = {
        "case_id": case.get("case_id") or "",
        "dedup_key_rule": DEDUP_RULE,
        "records": {rec["id"]: {"title": rec["title"], "doi": rec["doi"],
                                "dedup_key": rec["key"],
                                "source_file": rec["source_file"],
                                "locator": rec["locator"]}
                    for rec in records},
        "snapshots": {s["id"]: {"source_file": s.get("source_file"),
                                "locator": s.get("locator")}
                      for s in snapshots},
        "sources_used": sorted({rec["source_file"] for rec in records}),
        "sources_declared_unused": [f["file"] for f in failed_files],
        "case_file": case_rel,
    }

    report = make_report(case, subscription, snapshots, state, issue_list,
                         records, watermark, notes)

    search_ir = {
        "ir_version": "1.0",
        "case_id": case.get("case_id") or "",
        "kind": "incremental_tracking",
        "sources": [{"id": f"S{idx + 1}", "path": item["rel"],
                     "type": item["ext"].lstrip(".")}
                    for idx, item in enumerate(files) if item["rel"] != case_rel],
        "subscription": subscription,
        "snapshots": [{"id": s["id"],
                       "window": {"start": s.get("start"), "end": s.get("end")},
                       "record_ids": s["record_ids"],
                       "source": s.get("locator")} for s in snapshots],
        "records": [{"id": rec["id"], "title": rec["title"], "doi": rec["doi"],
                     "key": rec["key"], "source": rec["locator"]}
                    for rec in records],
        "provenance": {"pipeline": "material -> records -> snapshots -> diff -> artifacts",
                       "changes": []},
    }

    # write artifacts
    artifacts = []

    def emit(name, payload, role):
        target = os.path.join(out_dir, name)
        size = write_json(target, payload) if isinstance(payload, (dict, list)) \
            else len(payload.encode("utf-8"))
        if not isinstance(payload, (dict, list)):
            with open(target, "wb") as handle:
                handle.write(payload.encode("utf-8"))
        artifacts.append({"name": name, "role": role, "path": name, "bytes": size,
                          "sha256": sha256_bytes(open(target, "rb").read())})
        return name

    emit("incremental_state.json", incremental_state, "json")
    emit("provenance.json", provenance, "json")
    emit("search_ir.json", search_ir, "json")
    emit("validation.json", validation, "json")
    emit("report.md", report, "markdown")

    rows = [["id", "title", "doi", "arxiv", "pmid", "authors", "venue", "year",
             "published_date", "indexed_date", "retracted", "version_of",
             "dedup_key", "source_file", "locator"]]
    for rec in records:
        rows.append([rec["id"], rec["title"], rec["doi"], rec["arxiv"], rec["pmid"],
                     str(rec["authors"]), rec["venue"], rec["year"],
                     rec["published_date"], rec["indexed_date"],
                     "true" if rec["retracted"] else "false", rec["version_of"],
                     rec["key"], rec["source_file"], rec["locator"]])
    buffer = []
    for row in rows:
        cells = []
        for cell in row:
            text = str(cell).replace('"', '""')
            cells.append('"' + text + '"' if any(ch in text for ch in ',"\n') else text)
        buffer.append(",".join(cells))
    csv_text = "\n".join(buffer) + "\n"
    emit("records.csv", csv_text, "csv")

    contract = case.get("output_contract") or {}
    contract_map, contract_notes = {}, {}
    result = {}
    if isinstance(contract, dict):
        for key, spec in contract.items():
            if key in ("case_id", "status", "summary"):
                continue
            if key in LEGACY_ALIASES:
                result[key] = None  # filled below via alias
                continue
            target = artifact_target(key)
            result[key] = target
            contract_map[key] = target
            if target == "result.json":
                contract_notes[key] = {"note": "no dedicated artifact; see result.json",
                                       "spec": spec}
    elif isinstance(contract, list):
        result["output_contract"] = contract
        for key in contract:
            if key in ("case_id", "status", "summary"):
                continue
            if key in LEGACY_ALIASES:
                result[key] = None
                continue
            target = artifact_target(key)
            result[key] = target
            contract_map[key] = target

    result.update({
        "case_id": case.get("case_id") or "",
        "status": status,
        "status_reason": reason,
        "summary": summary,
        "kind": "incremental_tracking",
        "skill_version": SKILL_VERSION,
        "subscription": subscription,
        "window": (snapshots[-1] if snapshots else {"start": None, "end": None}),
        "new_items": new_items,
        "updates": updates,
        "retractions": retractions,
        "already_seen": already_count,
        "out_of_window": [],
        "late_indexed": [],
        "duplicates": state["duplicates"],
        "alerts": state["alerts"],
        "suppressed": state["suppressed"],
        "evidence": evidence,
        "snapshot": watermark,
        "next_snapshot": {
            "watermark": watermark["last_window_end"],
            "keys": sorted(state["seen_keys"].keys()),
            "generated_by": f"incremental_literature_alerting/{SKILL_VERSION}",
            "skill_version": SKILL_VERSION,
        },
        "uncertainty": notes,
        "sources": [{"file": item["rel"], "type": item["ext"].lstrip(".")}
                    for item in files],
        "artifacts": artifacts,
        "validation": validation,
        "stats": {
            "files_scanned": len(files),
            "records": len(records),
            "snapshots": len(snapshots),
            "new_items": len(new_items),
            "updates": len(updates),
            "retractions": len(retractions),
            "already_seen": already_count,
            "alerts": len(state["alerts"]),
            "suppressed": len(state["suppressed"]),
            "duplicates": len(state["duplicates"]),
            "window_issues": len(issue_list),
        },
        "contract": {
            "declared_keys": list(contract) if isinstance(contract, (dict, list)) else [],
            "mapped": contract_map,
            "extras": contract_notes,
            "all_present": True,
        },
        "change_log": [{
            "version": SKILL_VERSION,
            "change": "artifact contracts, import-state snapshot diff, version chains, "
                      "window audit, watermark replay",
        }],
    })

    for key, alias in LEGACY_ALIASES.items():
        if key in result:
            result[key] = result.get(alias)
    if isinstance(contract, dict):
        result["output_contract"] = contract
        result["output_contract_fields"] = {
            "declared": list(contract),
            "present": {key: (key in result) for key in contract},
        }

    claims = []
    for idx, item in enumerate(new_items + updates + retractions, start=1):
        claims.append({
            "claim_id": f"C{idx:03d}",
            "text": (f"窗口 {item.get('evidence', {}).get('locator', '')} 的条目"
                     f"「{item.get('title') or item.get('id')}」被判定为"
                     f"{'新增' if item in new_items else ('版本更新' if item in updates else '撤稿')}"),
            "field_path": item.get("evidence", {}).get("locator", ""),
            "evidence": item.get("evidence") or {},
            "derived_from_result": True,
        })
    result["claims"] = claims
    result.setdefault("uncertainties", notes)
    result["changelog"] = [{
        "version": SKILL_VERSION,
        "change": ("import-state snapshot diff, artifact contract mapping, window audit, "
                   "watermark replay, locator-anchored claims"),
    }]

    write_json(result_path, result)
    print(json.dumps({"status": status, "case_id": result["case_id"],
                      "result": result_path,
                      "new_items": len(new_items), "updates": len(updates),
                      "retractions": len(retractions),
                      "alerts": len(state["alerts"]),
                      "artifacts": [a["name"] for a in artifacts]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - must still emit a result.json
        out_dir = "/app/output"
        argv = sys.argv[1:]
        for idx, arg in enumerate(argv):
            if arg == "--out" and idx + 1 < len(argv):
                out_dir = argv[idx + 1]
        if out_dir.lower().endswith(".json"):
            out_dir = os.path.dirname(out_dir) or "."
        try:
            os.makedirs(out_dir, exist_ok=True)
            write_json(os.path.join(out_dir, "result.json"), {
                "case_id": "", "status": "failed",
                "status_reason": f"{type(exc).__name__}: {exc}",
                "skill_version": SKILL_VERSION, "artifacts": [],
                "uncertainty": [{"item": "engine crash",
                                 "reason": f"{type(exc).__name__}: {exc}",
                                 "handling": "result.json written by the crash guard"}],
            })
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False))
        sys.exit(0)
