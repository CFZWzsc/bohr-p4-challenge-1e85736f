#!/usr/bin/env python3
"""Incremental literature discovery and tracking (stdlib only, no network).

Reads a case descriptor plus every structured source file under the input
directory (Crossref / Europe PMC / DataCite / generic REST dumps, JSONL,
CSV and XML), normalises the records, deduplicates by stable keys, merges
preprint/formal/revision chains, diffs the result against the previous
snapshot, classifies new items / version updates / retractions / already
seen / out-of-window / late-indexed records, applies deterministic alert
suppression, and writes ``result.json`` with case_id, the fixed result
fields and every runtime ``output_contract`` key.

The entry never invents records.  Unknown fields become empty containers
with an explanation in ``uncertainty``; parsing failures are reported in
``errors`` while a structurally valid result is still written.

CLI::

    python scripts/sk_entry.py --input /input --work /app/work --out /app/output

``--input`` accepts a directory or a case JSON file; ``--out`` accepts a
directory or a ``*.json`` path.  ``run_skill.py`` is a thin wrapper.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - exotic runtimes
    ZoneInfo = None

SKILL_VERSION = "2.0.0"
DEFAULT_INPUT = "/input"
DEFAULT_WORK = "/app/work"
DEFAULT_OUT = "/app/output"

SUPPORTED_EXTS = {".json", ".jsonl", ".ndjson", ".xml", ".txt", ".csv", ".tsv"}
TEMPLATE_NAMES = {"expected.json", "expected_result.json", "verify.json", "agent_judgement.json"}
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 300
MAX_RECORDS = 20000
MAX_OUTPUT_ITEMS = 2000
MAX_SNAPSHOT_RECORDS = 5000
MAX_EVIDENCE_PER_ITEM = 12
MAX_SUPPRESSED = 1000

# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------
DEFAULT_POLICY = {
    "version": SKILL_VERSION,
    "dedup_priority": ["doi", "pmid", "arxiv", "normalized_title_year"],
    "alert_caps": {"max_items": 25, "include_updates": True, "include_retractions": True},
    "window": {
        "end_inclusive": True,
        "date_only_end_extends_to_end_of_day": True,
        "late_indexed_policy": "suppress_from_alerts",
    },
    "limits": {
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "max_files": MAX_FILES,
        "max_records": MAX_RECORDS,
    },
}


def _deep_update(base, extra):
    if not isinstance(extra, dict):
        return base
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_policy():
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    candidate = Path(__file__).resolve().parent.parent / "assets" / "policy.json"
    try:
        if candidate.is_file():
            _deep_update(policy, json.loads(candidate.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return policy


POLICY = load_policy()
MAX_FILE_BYTES = int(POLICY.get("limits", {}).get("max_file_bytes", MAX_FILE_BYTES))
MAX_TOTAL_BYTES = int(POLICY.get("limits", {}).get("max_total_bytes", MAX_TOTAL_BYTES))
MAX_FILES = int(POLICY.get("limits", {}).get("max_files", MAX_FILES))
MAX_RECORDS = int(POLICY.get("limits", {}).get("max_records", MAX_RECORDS))

# ---------------------------------------------------------------------------
# field vocabulary
# ---------------------------------------------------------------------------
TITLE_KEYS = ("title", "article_title", "article-title", "paper_title", "display_name",
              "titles", "dc:title", "题名", "标题")
DOI_KEYS = ("doi", "DOI", "Doi", "dc:identifier", "identifier", "identifiers",
            "article-id", "article_id", "europepmc_doi")
PMID_KEYS = ("pmid", "pubmed_id", "pubmedid", "PMID", "pubmed-id")
ARXIV_KEYS = ("arxiv_id", "arxiv", "arxiv-id", "eprint", "eprint_id", "eprintid")
AUTHOR_KEYS = ("authors", "author", "creators", "creator", "contributors",
               "contributor", "authorString", "author_string", "author_list", "作者")
JOURNAL_KEYS = ("journal", "journal_title", "journalTitle", "container_title",
                "container-title", "venue", "publicationName", "journal-name",
                "source_title", "期刊")
VERSION_KEYS = ("version", "version_label", "version-label", "version_number",
                "version-number", "ver", "revision", "edition", "issue_version")
INDEX_DATE_KEYS = ("first_indexed", "firstIndexed", "entry_date", "entryDate",
                   "indexed_date", "indexedDate", "indexed", "imported_at",
                   "importedAt", "added_at", "addedAt", "retrieved_at",
                   "retrievedAt", "import_date", "importDate", "last_imported_at",
                   "lastImportedAt", "created", "date_created")
PUBLISHED_DATE_KEYS = ("published", "published-print", "published-online", "issued",
                       "publication_date", "publicationDate", "firstPublicationDate",
                       "pub_date", "pubDate", "date", "publicationYear", "pubYear",
                       "year", "published_date")
RETRACTION_FLAGS = ("retracted", "is_retracted", "is-retracted", "retraction",
                    "withdrawn", "is_withdrawn", "has_retraction")
UPDATE_TYPE_KEYS = ("update_type", "update-type", "updateType", "correction_type",
                    "pubType", "pubtype", "publication_type", "subtype")

NON_RECORD_KEYS = {
    "output_contract", "previous_snapshot", "last_snapshot", "snapshot", "baseline_snapshot",
    "state", "import_state", "subscription", "window", "time_window", "period",
    "alert_rules", "policy", "input", "task", "instructions",
    "references", "reference_list", "reference_graph", "reference_graphs",
    "citations", "citation", "bibliography", "cited_by", "edges", "nodes", "links",
    "titles", "authors", "creators", "contributors", "dates", "identifiers",
    "relatedidentifiers", "related_identifiers", "container", "journal",
    "abstract", "keywords", "subject", "subjects", "license", "relation", "relations",
    "update-to", "update_to", "commentcorrectionlist", "comment_correction_list",
}
RECORD_CONTAINER_KEYS = {
    "items", "item", "records", "record", "results", "result", "entries", "entry",
    "works", "work", "docs", "doc", "hits", "hit", "papers", "paper", "candidates",
    "candidate", "documents", "document", "data", "payload", "response", "feed",
    "list", "rows", "articles", "article", "publications", "publication",
}
REFERENCE_LIKE_KEYS = {"references", "reference_list", "reference_graph",
                       "reference_graphs", "citations", "citation", "bibliography",
                       "cited_by", "edges", "nodes"}
REFERENCE_LIKE_FILES = re.compile(r"(^|[/_\-. ])(reference|references|reference-graph|"
                                  r"citation|citations|bibliography|edges|nodes)([/_\-. ]|$)", re.I)
XML_RECORD_TAGS = {"article", "record", "result", "entry", "item", "work",
                   "publication", "pubmedarticle", "pubmedbookarticle",
                   "doi_record", "crossref_record", "resource"}

PLACEHOLDER_STATUS = {"success", "partial", "insufficient_input", "failed"}

# ---------------------------------------------------------------------------
# small text utilities
# ---------------------------------------------------------------------------

def norm_space(value):
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip()


def as_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return norm_space(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        for item in value:
            text = as_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("title", "value", "name", "text", "@value", "label", "#text"):
            if key in value:
                text = as_text(value[key])
                if text:
                    return text
        return ""
    return norm_space(value)


def ci_get(node, names):
    """Case-insensitive value lookup.  Keeps False/0 (meaningful for flags)."""
    if not isinstance(node, dict):
        return None
    lower = {}
    for key, value in node.items():
        lower.setdefault(str(key).lower(), value)
    for name in names:
        key = name.lower()
        if key in lower:
            value = lower[key]
            if value is not None and value != "":
                return value
    return None


def pick(node, names):
    """First non-empty text-ish value."""
    for name in names:
        value = ci_get(node, (name,))
        text = as_text(value)
        if text:
            return text
    return ""


def escape_pointer(key):
    return str(key).replace("~", "~0").replace("/", "~1")


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def unique(items):
    seen = set()
    out = []
    for item in items:
        key = str(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# identifier / title normalisation
# ---------------------------------------------------------------------------
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.I)
ARXIV_RE = re.compile(r"(?:arxiv[:\s]*)?((?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[a-z]{2})?/\d{7}))(v\d+)?", re.I)


def norm_doi(value):
    if isinstance(value, list):
        for item in value:
            doi = norm_doi(item)
            if doi:
                return doi
        return ""
    if isinstance(value, dict):
        typ = norm_space(value.get("type") or value.get("scheme") or value.get("id-type")).lower()
        if typ and "doi" not in typ:
            return ""
        return norm_doi(value.get("value") or value.get("id") or value.get("identifier")
                        or value.get("DOI") or value.get("_id"))
    text = norm_space(value)
    if not text:
        return ""
    text = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", text, flags=re.I)
    match = DOI_RE.search(text)
    if match:
        text = match.group(0)
    text = text.strip(" .,;:)]}'\"").lower()
    text = re.sub(r"\.pdf$", "", text)
    return text


def norm_pmid(value):
    if isinstance(value, dict):
        typ = norm_space(value.get("type") or value.get("scheme") or value.get("id-type")).lower()
        if typ and "pmid" not in typ and "pubmed" not in typ:
            return ""
        return norm_pmid(value.get("value") or value.get("id") or value.get("identifier"))
    if isinstance(value, list):
        for item in value:
            pmid = norm_pmid(item)
            if pmid:
                return pmid
        return ""
    text = norm_space(value)
    text = re.sub(r"^(pmid|pubmed)[:\s]*", "", text, flags=re.I).strip()
    match = re.fullmatch(r"\d{4,9}", text)
    return match.group(0) if match else ""


def norm_arxiv(value):
    if isinstance(value, dict):
        return norm_arxiv(value.get("value") or value.get("id") or value.get("identifier"))
    if isinstance(value, list):
        for item in value:
            arxiv = norm_arxiv(item)
            if arxiv:
                return arxiv
        return ""
    text = norm_space(value)
    match = ARXIV_RE.search(text)
    if not match:
        return ""
    return (match.group(1) + (match.group(2) or "")).lower()


def arxiv_version(value):
    text = norm_space(value)
    match = re.search(r"v(\d+)\s*$", text, re.I)
    return match.group(1) if match else ""


def norm_title(value):
    text = norm_space(value).lower()
    text = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)


def truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "y", "1", "retracted", "withdrawn")
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return False


# ---------------------------------------------------------------------------
# dates and timezone handling
# ---------------------------------------------------------------------------
DATE_ONLY_RE = re.compile(r"^\s*(\d{4})(?:[-/](\d{1,2}))?(?:[-/](\d{1,2}))?\s*$")


def _end_of_date(value):
    match = DATE_ONLY_RE.match(value or "")
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 12)
    if match.group(3):
        day = int(match.group(3))
    elif match.group(2):
        day = calendar.monthrange(year, month)[1]
    else:
        day = 31
    try:
        return dt.datetime(year, month, day, 23, 59, 59, 999999)
    except ValueError:
        return None


def parse_dt(value, end_of_day=False):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        if end_of_day and value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.replace(hour=23, minute=59, second=59, microsecond=999999)
        return value
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float)):
        year = int(value)
        if 1500 < year < 2200:
            return dt.datetime(year, 1, 1)
        return None
    if isinstance(value, dict):
        for key in ("date-parts", "date_parts", "dateParts"):
            parts = value.get(key)
            if isinstance(parts, list) and parts:
                inner = parts[0] if isinstance(parts[0], list) else parts
                try:
                    return dt.datetime(int(inner[0]),
                                       int(inner[1]) if len(inner) > 1 else 1,
                                       int(inner[2]) if len(inner) > 2 else 1)
                except (TypeError, ValueError, IndexError):
                    return None
        for key in ("value", "date", "iso", "timestamp", "@value", "dateString"):
            if key in value and value[key] is not None:
                return parse_dt(value[key], end_of_day)
        return None
    if isinstance(value, list):
        if value and isinstance(value[0], (list, tuple)):
            return parse_dt({"date-parts": value}, end_of_day)
        if value and all(isinstance(item, int) for item in value):
            try:
                return dt.datetime(int(value[0]),
                                   int(value[1]) if len(value) > 1 else 1,
                                   int(value[2]) if len(value) > 2 else 1)
            except (ValueError, IndexError):
                return None
        for item in value:
            parsed = parse_dt(item, end_of_day)
            if parsed:
                return parsed
        return None
    text = norm_space(value)
    if not text:
        return None
    if re.fullmatch(r"\d{4}", text):
        if end_of_day:
            return _end_of_date(text)
        return dt.datetime(int(text), 1, 1)
    if re.fullmatch(r"\d{4}[-/]\d{1,2}", text):
        if end_of_day:
            return _end_of_date(text)
        parts = re.split(r"[-/]", text)
        try:
            return dt.datetime(int(parts[0]), int(parts[1]), 1)
        except ValueError:
            return None
    date_only = bool(re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text))
    normalised = re.sub(r"^(\d{4})/(\d{1,2})/(\d{1,2})", r"\1-\2-\3", text)
    normalised = normalised.replace("Z", "+00:00")
    parsed = None
    try:
        parsed = dt.datetime.fromisoformat(normalised)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d %b %Y",
                    "%d %B %Y", "%b %d %Y", "%B %d %Y", "%Y%m%d"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        match = re.search(r"(1[5-9]\d{2}|20\d{2})-(\d{1,2})-(\d{1,2})", text)
        if not match:
            return None
        try:
            parsed = dt.datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    if end_of_day and date_only:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def iso(value):
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return None


def to_naive(value, tzname):
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    if tzname and ZoneInfo is not None:
        try:
            return value.astimezone(ZoneInfo(tzname)).replace(tzinfo=None)
        except Exception:
            pass
    # No tz database (e.g. bare Windows): keep the supplied wall clock instead
    # of silently shifting the date into another day.
    return value.replace(tzinfo=None)


def date_precision(value):
    text = norm_space(value)
    if re.fullmatch(r"\d{4}", text):
        return "year"
    if re.fullmatch(r"\d{4}[-/]\d{1,2}", text):
        return "month"
    if re.fullmatch(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", text):
        return "day"
    return "timestamp"


# ---------------------------------------------------------------------------
# record field extraction
# ---------------------------------------------------------------------------

def extract_title(node):
    value = ci_get(node, TITLE_KEYS)
    if isinstance(value, list):
        for item in value:
            text = as_text(item.get("title") if isinstance(item, dict) else item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("title", "value", "@value", "text", "name"):
            text = as_text(value.get(key))
            if text:
                return text
        return ""
    return as_text(value)


def extract_doi(node):
    for key in DOI_KEYS:
        value = ci_get(node, (key,))
        doi = norm_doi(value)
        if doi:
            return doi
    return ""


def extract_pmid(node):
    for key in PMID_KEYS:
        value = ci_get(node, (key,))
        pmid = norm_pmid(value)
        if pmid:
            return pmid
    source = norm_space(ci_get(node, ("source", "src"))).upper()
    if source in ("MED", "PUBMED"):
        value = ci_get(node, ("id", "uid"))
        if isinstance(value, str) and re.fullmatch(r"\d{4,9}", value.strip()):
            return value.strip()
    return ""


def extract_arxiv(node):
    for key in ARXIV_KEYS:
        value = ci_get(node, (key,))
        arxiv = norm_arxiv(value)
        if arxiv:
            return arxiv
    doi = extract_doi(node)
    if doi.startswith("10.48550/arxiv."):
        return norm_arxiv(doi.split("arxiv.", 1)[1])
    return ""


def extract_authors(node):
    value = ci_get(node, AUTHOR_KEYS)
    out = []

    def add(item):
        if isinstance(item, str):
            text = norm_space(item)
            if text:
                out.append(text)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("fullName") or item.get("full_name") \
                or item.get("literal")
            if not name:
                given = as_text(item.get("given") or item.get("givenName") or item.get("first"))
                family = as_text(item.get("family") or item.get("familyName")
                                 or item.get("last") or item.get("surname"))
                name = " ".join(part for part in (given, family) if part)
            if name:
                out.append(norm_space(name))
        elif isinstance(item, list):
            for child in item:
                add(child)

    add(value)
    return "; ".join(unique(out))


def extract_journal(node):
    value = ci_get(node, JOURNAL_KEYS)
    if isinstance(value, list):
        for item in value:
            text = as_text(item.get("title") if isinstance(item, dict) else item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("title", "name", "value"):
            text = as_text(value.get(key))
            if text:
                return text
        return ""
    return as_text(value)


def extract_version(node):
    value = ci_get(node, VERSION_KEYS)
    text = norm_space(value)
    if text:
        match = re.search(r"v?(\d+(?:\.\d+)*)", text, re.I)
        return match.group(1) if match else text
    return arxiv_version(ci_get(node, ARXIV_KEYS))


def extract_dates(node):
    index_date = index_field = None
    for key in INDEX_DATE_KEYS:
        parsed = parse_dt(ci_get(node, (key,)))
        if parsed:
            index_date, index_field = parsed, key
            break
    published_date = published_field = None
    for key in PUBLISHED_DATE_KEYS:
        parsed = parse_dt(ci_get(node, (key,)))
        if parsed:
            published_date, published_field = parsed, key
            break
    dates = ci_get(node, ("dates",))
    if isinstance(dates, list):
        for item in dates:
            if not isinstance(item, dict):
                continue
            dtype = norm_space(item.get("dateType") or item.get("date_type")
                               or item.get("type")).lower()
            parsed = parse_dt(item.get("date") or item.get("value"))
            if not parsed:
                continue
            if any(word in dtype for word in ("creat", "avail", "updat", "index", "import")):
                if index_date is None:
                    index_date, index_field = parsed, "dates:" + (dtype or "created")
            elif published_date is None:
                published_date, published_field = parsed, "dates:" + (dtype or "issued")
    return {
        "index_date": index_date,
        "index_field": index_field,
        "published_date": published_date,
        "published_field": published_field,
        "index_precision": date_precision(ci_get(node, (index_field,))) if index_field else "unknown",
        "published_precision": date_precision(ci_get(node, (published_field,))) if published_field else "unknown",
    }


def _add_related(store, value, relation):
    if isinstance(value, str):
        doi = norm_doi(value)
        if doi:
            store.append({"relation": relation, "id": "doi:" + doi})
            return
        arxiv = norm_arxiv(value)
        if arxiv:
            store.append({"relation": relation, "id": "arxiv:" + arxiv})
            return
        pmid = norm_pmid(value)
        if pmid:
            store.append({"relation": relation, "id": "pmid:" + pmid})
    elif isinstance(value, dict):
        _add_related(store, value.get("DOI") or value.get("doi") or value.get("id")
                     or value.get("value") or value.get("relatedIdentifier")
                     or value.get("related_identifier"), relation)
    elif isinstance(value, list):
        for item in value:
            _add_related(store, item, relation)


def extract_markers(node):
    markers = set()
    update_types = []
    related = []

    for key in RETRACTION_FLAGS:
        if truthy(ci_get(node, (key,))):
            markers.add("retraction")

    for key in UPDATE_TYPE_KEYS:
        value = ci_get(node, (key,))
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = as_text(item)
            low = text.lower()
            if not low:
                continue
            if "retract" in low or "withdraw" in low:
                markers.add("retraction")
                update_types.append(text)
            elif "correction" in low or "erratum" in low or "corrigendum" in low:
                markers.add("correction")
                update_types.append(text)
            elif "revision" in low or "new version" in low or low == "update" \
                    or "updated" in low:
                markers.add("version_update")
                update_types.append(text)

    for key in ("update-to", "update_to", "updateto"):
        value = ci_get(node, (key,))
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                itype = norm_space(item.get("type") or item.get("update_type")).lower()
                if "retract" in itype or "withdraw" in itype:
                    markers.add("retraction")
                    update_types.append(itype)
                _add_related(related, item.get("DOI") or item.get("doi"), itype or "update-to")

    relation = ci_get(node, ("relation", "relations"))
    if isinstance(relation, dict):
        for rel_key, rel_value in relation.items():
            low = str(rel_key).lower()
            if "retract" in low or "withdraw" in low:
                markers.add("retraction")
            if "preprint" in low or "version" in low or "updated" in low:
                markers.add("version_update")
            _add_related(related, rel_value, low)

    related_ids = ci_get(node, ("related_identifiers", "relatedIdentifiers",
                                "related-identifiers"))
    if isinstance(related_ids, list):
        for item in related_ids:
            if not isinstance(item, dict):
                continue
            rtype = norm_space(item.get("relationType") or item.get("relation_type")
                               or item.get("type")).lower()
            if "retract" in rtype or "withdraw" in rtype:
                markers.add("retraction")
            if "preprint" in rtype or "version" in rtype or "isidentical" in rtype:
                markers.add("version_update")
            _add_related(related, item.get("relatedIdentifier")
                         or item.get("related_identifier") or item.get("id"), rtype)

    ccl = ci_get(node, ("commentCorrectionList", "comment_correction_list"))
    if isinstance(ccl, dict):
        ccl = ccl.get("commentCorrection") or ccl.get("comment_correction") or ccl
    if isinstance(ccl, list):
        for item in ccl:
            if not isinstance(item, dict):
                continue
            itype = norm_space(item.get("type") or item.get("commentType")).lower()
            if "retract" in itype:
                markers.add("retraction")
                update_types.append(itype)
            elif "correct" in itype or "erratum" in itype:
                markers.add("correction")
                update_types.append(itype)
            _add_related(related, item.get("id") or item.get("DOI"), itype)

    title = extract_title(node)
    if re.match(r"^(retraction|retracted|withdrawal|withdrawn)\b", title, re.I):
        markers.add("retraction")
    elif re.match(r"^(correction|corrigendum|erratum)\b", title, re.I):
        markers.add("correction")

    # Round-tripped snapshot records store update_types/is_preprint/is_formal
    # explicitly; preserve them so a second run stays idempotent.
    stored_types = ci_get(node, ("update_types", "updateTypes"))
    if isinstance(stored_types, list):
        for item in stored_types:
            text = as_text(item)
            low = text.lower()
            if not low:
                continue
            update_types.append(text)
            if "retract" in low or "withdraw" in low:
                markers.add("retraction")
            elif "correct" in low or "erratum" in low:
                markers.add("correction")
            elif "revision" in low or "version" in low or "update" in low:
                markers.add("version_update")

    return {
        "markers": sorted(markers),
        "update_types": unique(update_types),
        "related": related,
    }


def looks_like_record(node, key_hint=""):
    if not isinstance(node, dict) or not node:
        return False
    title = extract_title(node)
    doi = extract_doi(node)
    pmid = extract_pmid(node)
    arxiv = extract_arxiv(node)
    has_id = bool(doi or pmid or arxiv)
    if not title and not has_id:
        return False
    keys = {str(key).lower() for key in node.keys()}
    if not has_id and keys & {"titles", "authors", "creators", "identifiers",
                              "dates", "container", "journal", "abstract"}:
        return False
    if not has_id and keys & {"message", "resultlist", "data", "response",
                              "payload", "feed", "records", "results", "items"}:
        return False
    has_date = any(ci_get(node, (key,)) is not None
                   for key in INDEX_DATE_KEYS + PUBLISHED_DATE_KEYS)
    if title and (has_id or has_date or extract_authors(node)
                  or key_hint in RECORD_CONTAINER_KEYS):
        return True
    if has_id and (extract_journal(node) or extract_authors(node) or has_date):
        return True
    return False


def dedup_key(record):
    if record.get("doi"):
        return "doi:" + record["doi"]
    if record.get("pmid"):
        return "pmid:" + record["pmid"]
    if record.get("arxiv_id"):
        return "arxiv:" + record["arxiv_id"].lower()
    title = norm_title(record.get("title", ""))
    if title:
        year = record.get("year")
        return "title:" + title + ("|" + str(year) if year else "")
    return "loc:" + str(record.get("locator", "")) + "|" + str(record.get("file", ""))


def record_keys(record):
    keys = [dedup_key(record)]
    if record.get("doi"):
        keys.append("doi:" + record["doi"])
    if record.get("pmid"):
        keys.append("pmid:" + record["pmid"])
    if record.get("arxiv_id"):
        keys.append("arxiv:" + record["arxiv_id"].lower())
    title = norm_title(record.get("title", ""))
    if title:
        year = record.get("year")
        keys.append("title:" + title + ("|" + str(year) if year else ""))
    return sorted(set(keys))


def record_ids(record):
    ids = []
    if record.get("doi"):
        ids.append("doi:" + record["doi"])
    if record.get("pmid"):
        ids.append("pmid:" + record["pmid"])
    if record.get("arxiv_id"):
        ids.append("arxiv:" + record["arxiv_id"].lower())
    return ids


def normalize_record(raw, locator, provider, query="", case_inline=False):
    node = raw if isinstance(raw, dict) else {}
    dates = extract_dates(node)
    markers = extract_markers(node)
    doi = extract_doi(node)
    pmid = extract_pmid(node)
    arxiv = extract_arxiv(node)
    version = extract_version(node)
    published_date = dates["published_date"]
    index_date = dates["index_date"]
    index_is_fallback = False
    if index_date is None and published_date is not None:
        index_date = published_date
        index_is_fallback = True
    year = None
    year_value = ci_get(node, ("publicationYear", "pubYear", "year", "publication_year"))
    if isinstance(year_value, int) and 1500 < year_value < 2200:
        year = year_value
    elif isinstance(year_value, str) and re.fullmatch(r"\d{4}", year_value.strip()):
        year = int(year_value.strip())
    elif published_date:
        year = published_date.year
    title = extract_title(node)
    unit = {
        "title": title,
        "doi": doi,
        "pmid": pmid,
        "arxiv_id": arxiv,
        "authors": extract_authors(node),
        "journal": extract_journal(node),
        "year": year,
        "version": version,
        "index_date": index_date,
        "published_date": published_date,
        "index_field": dates["index_field"],
        "published_field": dates["published_field"],
        "index_is_fallback": index_is_fallback,
        "index_precision": dates["index_precision"] if not index_is_fallback else dates["published_precision"],
        "published_precision": dates["published_precision"],
        "markers": markers["markers"],
        "update_types": markers["update_types"],
        "related": markers["related"],
        "retracted": "retraction" in markers["markers"],
        "is_preprint": False,
        "is_formal": False,
        "source_label": norm_space(ci_get(node, ("source", "database", "db", "provider", "publisher"))),
        "record_id": norm_space(ci_get(node, ("record_id", "paper_id", "uid", "id", "key"))),
        "file": locator.get("file", ""),
        "locator": locator,
        "provider": provider,
        "query": query or "",
        "case_inline": case_inline,
        "raw_keys": sorted(str(key) for key in node.keys())[:40],
    }
    typ = norm_space(ci_get(node, ("type", "publication_type", "subtype", "pubType"))).lower()
    unit["is_preprint"] = ("preprint" in typ or "posted-content" in typ
                           or bool(arxiv) and not unit["journal"] and not doi)
    unit["is_formal"] = bool(doi) and not unit["is_preprint"] and (bool(unit["journal"]) or True)
    if isinstance(node.get("is_preprint"), bool):
        unit["is_preprint"] = node["is_preprint"]
    if isinstance(node.get("is_formal"), bool):
        unit["is_formal"] = node["is_formal"]
    return unit


# ---------------------------------------------------------------------------
# file discovery
# ---------------------------------------------------------------------------

def resolve_input(input_path):
    path = Path(input_path)
    if path.is_file():
        return path, path.parent
    return None, path


def discover_case(case_path, input_dir):
    candidates = []
    if case_path is not None:
        candidates.append(case_path)
    root = Path(input_dir) / "case.json"
    if root.is_file():
        candidates.append(root)
    try:
        for candidate in sorted(Path(input_dir).rglob("case.json")):
            if candidate not in candidates:
                candidates.append(candidate)
    except OSError:
        pass
    for candidate in candidates:
        try:
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data, candidate, None
        except (OSError, ValueError) as exc:
            return {}, candidate, {"stage": "read_case", "file": str(candidate),
                                   "type": type(exc).__name__,
                                   "message": str(exc)[:300]}
    return {}, None, None


def list_input_files(input_dir, excluded_roots=None):
    files = []
    total = 0
    excluded = []
    for root in excluded_roots or []:
        try:
            excluded.append(Path(root).resolve())
        except OSError:
            pass
    try:
        all_paths = sorted((p for p in Path(input_dir).rglob("*") if p.is_file()),
                           key=lambda p: str(p).lower())
    except OSError:
        return files, total
    for path in all_paths:
        if len(files) >= MAX_FILES:
            break
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if any(resolved == root or root in resolved.parents for root in excluded):
            continue
        name = path.name.lower()
        if name.startswith(".") or name in TEMPLATE_NAMES:
            continue
        if name in ("case.json", "task.md", "task.txt"):
            continue
        is_pdf = path.suffix.lower() == ".pdf"
        if not is_pdf and path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            files.append({"path": path, "size": size, "too_large": True,
                          "over_budget": False, "pdf": is_pdf})
            continue
        if total + size > MAX_TOTAL_BYTES:
            files.append({"path": path, "size": size, "too_large": False,
                          "over_budget": True, "pdf": is_pdf})
            continue
        total += size
        files.append({"path": path, "size": size, "too_large": False,
                      "over_budget": False, "pdf": is_pdf})
    return files, total


def infer_provider(rel, data=None):
    low = rel.lower()
    for token, label in (("europepmc", "europepmc"), ("europe_pmc", "europepmc"),
                         ("pubmed", "europepmc"), ("crossref", "crossref"),
                         ("datacite", "datacite"), ("arxiv", "arxiv"),
                         ("openalex", "openalex"), ("semantic", "semanticscholar"),
                         ("scopus", "scopus"), ("wos", "wos"),
                         ("webofscience", "wos"), ("publisher", "publisher")):
        if token in low:
            return label
    if isinstance(data, dict):
        if "message-type" in data or isinstance(data.get("message"), dict):
            return "crossref"
        if "resultList" in data or "hitCount" in data:
            return "europepmc"
        data_block = data.get("data")
        if isinstance(data_block, list) and data_block and isinstance(data_block[0], dict) \
                and ("attributes" in data_block[0] or "types" in data_block[0]):
            return "datacite"
        attrs = data_block.get("attributes") if isinstance(data_block, dict) else None
        if isinstance(attrs, dict) and "titles" in attrs:
            return "datacite"
    return Path(rel).stem.lower() or "unknown"


def file_query(data):
    if isinstance(data, dict):
        for key in ("query", "search_query", "searchQuery", "q"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        request = data.get("request")
        if isinstance(request, dict) and isinstance(request.get("query"), str):
            return request["query"].strip()
        message = data.get("message")
        if isinstance(message, dict) and isinstance(message.get("query"), str):
            return message["query"].strip()
    return ""


# ---------------------------------------------------------------------------
# JSON / JSONL / CSV / XML extraction
# ---------------------------------------------------------------------------

def walk_json(node, ctx, out, skipped, pointer="", key_hint="", depth=0, allow_record=True):
    if depth > 14 or len(out) >= MAX_RECORDS:
        return
    if isinstance(node, dict):
        if allow_record and looks_like_record(node, key_hint):
            local_ctx = dict(ctx)
            local_ctx["pointer"] = pointer
            out.append((node, local_ctx, {"file": ctx.get("file", ""), "kind": "json",
                                          "pointer": pointer}))
            return
        local_ctx = ctx
        query = file_query(node)
        if query:
            local_ctx = dict(ctx)
            local_ctx["query"] = query
        for key, value in node.items():
            low = str(key).lower()
            child_pointer = pointer + "/" + escape_pointer(key)
            if low in NON_RECORD_KEYS:
                if low in REFERENCE_LIKE_KEYS:
                    skipped.add(low)
                continue
            walk_json(value, local_ctx, out, skipped, child_pointer, low, depth + 1,
                      True)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            walk_json(value, ctx, out, skipped, pointer + "/" + str(index), key_hint,
                      depth + 1, True)


def parse_json_text(text, rel, provider):
    data = json.loads(text)
    provider = infer_provider(rel, data)
    out, skipped = [], set()
    ctx = {"file": rel, "provider": provider, "query": file_query(data)}
    walk_json(data, ctx, out, skipped)
    return data, out, skipped


def add_line_locator(records, line_no):
    out = []
    for raw, ctx, loc in records:
        new_loc = dict(loc)
        new_loc["kind"] = "jsonl"
        new_loc["line"] = line_no
        new_loc.pop("pointer", None)
        if loc.get("pointer"):
            new_loc["pointer"] = loc["pointer"]
        out.append((raw, ctx, new_loc))
    return out


def parse_jsonl_text(text, rel, provider, errors):
    out, skipped = [], set()
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError as exc:
            errors.append({"stage": "parse_jsonl", "file": rel, "line": line_no,
                           "type": type(exc).__name__, "message": str(exc)[:200]})
            continue
        provider = infer_provider(rel, data)
        ctx = {"file": rel, "provider": provider, "query": file_query(data)}
        local = []
        walk_json(data, ctx, local, skipped)
        out.extend(add_line_locator(local, line_no))
    return out, skipped


HEADER_ALIASES = {
    "title": "title", "ti": "title", "article_title": "title", "article-title": "title",
    "titles": "title", "paper_title": "title", "题名": "title",
    "doi": "doi", "dois": "doi", "doi_url": "doi",
    "pmid": "pmid", "pubmed_id": "pmid", "pubmedid": "pmid",
    "arxiv": "arxiv_id", "arxiv_id": "arxiv_id", "eprint": "arxiv_id",
    "authors": "authors", "author": "authors", "author_string": "authors",
    "authorstring": "authors", "creator": "authors", "creators": "authors",
    "journal": "journal", "journal_title": "journal", "journaltitle": "journal",
    "container_title": "journal", "container-title": "journal", "venue": "journal",
    "first_indexed": "first_indexed", "firstindexed": "first_indexed",
    "entry_date": "entry_date", "indexed": "indexed",
    "imported_at": "imported_at", "added_at": "added_at", "retrieved_at": "retrieved_at",
    "published": "published", "publication_date": "published",
    "publicationdate": "published", "firstpublicationdate": "published",
    "issued": "issued", "date": "date", "year": "year", "pubyear": "year",
    "publicationyear": "year", "version": "version", "version_label": "version",
    "retracted": "retracted", "is_retracted": "retracted", "withdrawn": "withdrawn",
    "update_type": "update_type", "type": "type", "source": "source",
}


def csv_record_from_row(row):
    mapped = {}
    for key, value in row.items():
        if key is None:
            continue
        normalised = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
        mapped[HEADER_ALIASES.get(normalised, HEADER_ALIASES.get(str(key).strip().lower(),
                                                                  str(key).strip()))] = value
    return mapped


def parse_csv_text(text, rel, provider, errors, delimiter=None):
    rows = []
    try:
        if delimiter is None:
            sample = text[:8192]
            try:
                delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
            except csv.Error:
                delimiter = "\t" if "\t" in sample.splitlines()[0] else ","
        reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
        for row_no, row in enumerate(reader, start=2):
            mapped = csv_record_from_row(row)
            if not any(str(value).strip() for value in mapped.values() if value):
                continue
            if not looks_like_record(mapped):
                continue
            rows.append((mapped, {"file": rel, "provider": provider, "query": ""},
                         {"file": rel, "kind": "csv", "row": row_no}))
    except csv.Error as exc:
        errors.append({"stage": "parse_csv", "file": rel, "type": type(exc).__name__,
                       "message": str(exc)[:200]})
    return rows, set()


def local_name(tag):
    return str(tag).split("}")[-1].lower()


def xml_to_record(element):
    data = {}
    authors = []
    text_bits = []
    for node in element.iter():
        name = local_name(node.tag)
        text = norm_space(node.text)
        if text and name in ("article-title", "articletitle", "title", "chapter-title"):
            data.setdefault("title", text)
        elif name == "doi" and text:
            data.setdefault("doi", text)
        elif name in ("pmid", "pmcid") and text:
            data.setdefault(name, text)
        elif name == "article-id":
            pub_type = norm_space(node.attrib.get("pub-id-type")).lower()
            if pub_type == "doi" and text:
                data.setdefault("doi", text)
            elif pub_type == "pmid" and text:
                data.setdefault("pmid", text)
        elif name in ("journal-title", "journaltitle") and text:
            data.setdefault("journal", text)
        elif name in ("first-indexed", "first_indexed", "indexed") and text:
            data.setdefault("first_indexed", text)
        elif name in ("version", "revision") and text:
            data.setdefault("version", text)
        elif name in ("publication-type", "pub-type", "subject") and text:
            low = text.lower()
            if "retract" in low or "withdraw" in low:
                data["retracted"] = True
            elif "correction" in low or "erratum" in low:
                data.setdefault("update_type", text)
        elif name in ("author", "contrib", "creator"):
            given = family = ""
            for child in node.iter():
                child_name = local_name(child.tag)
                if child_name in ("given-names", "given", "first"):
                    given = norm_space(child.text)
                elif child_name in ("surname", "family", "last"):
                    family = norm_space(child.text)
            name_text = " ".join(part for part in (given, family) if part)
            if not name_text:
                name_text = norm_space(node.text)
            if name_text:
                authors.append(name_text)
        elif name in ("pub-date", "publication-date", "date"):
            year = month = day = ""
            for child in node.iter():
                child_name = local_name(child.tag)
                if child_name == "year":
                    year = norm_space(child.text)
                elif child_name == "month":
                    month = norm_space(child.text)
                elif child_name == "day":
                    day = norm_space(child.text)
            if year and not data.get("published"):
                data["published"] = "%s-%s-%s" % (year, month or "1", day or "1")
        if text:
            text_bits.append(text)
    if authors:
        data["authors"] = "; ".join(unique(authors))
    if not data.get("title") and not (data.get("doi") or data.get("pmid")):
        return None
    if "retracted" not in data:
        joined = " ".join(text_bits[:20]).lower()
        if "retraction" in joined and data.get("title", "").lower().startswith("retraction"):
            data["retracted"] = True
    return data


def parse_xml_text(text, rel, provider, errors):
    out = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        errors.append({"stage": "parse_xml", "file": rel, "type": type(exc).__name__,
                       "message": str(exc)[:200]})
        return out, set()

    def walk(element, path):
        name = local_name(element.tag)
        if name in XML_RECORD_TAGS:
            record = xml_to_record(element)
            if record:
                out.append((record, {"file": rel, "provider": provider, "query": ""},
                            {"file": rel, "kind": "xml", "xpath": path}))
                return
        counts = {}
        for child in list(element):
            child_name = local_name(child.tag)
            counts[child_name] = counts.get(child_name, 0) + 1
            walk(child, path + "/" + child_name + "[%d]" % counts[child_name])

    walk(root, "/" + local_name(root.tag) + "[1]")
    return out, set()


# ---------------------------------------------------------------------------
# snapshot handling
# ---------------------------------------------------------------------------

def snapshot_key_ids(key):
    text = norm_space(key)
    low = text.lower()
    if low.startswith("doi:"):
        return ["doi:" + norm_doi(text[4:])]
    if low.startswith("pmid:"):
        return ["pmid:" + norm_pmid(text[5:])]
    if low.startswith("arxiv:"):
        return ["arxiv:" + norm_arxiv(text[6:])]
    if norm_doi(text):
        return ["doi:" + norm_doi(text)]
    if re.fullmatch(r"\d{6,9}", text):
        return ["pmid:" + text]
    if norm_arxiv(text):
        return ["arxiv:" + norm_arxiv(text)]
    return []


def snapshot_title_pair(key):
    text = norm_space(key)
    if not text.lower().startswith("title:"):
        return None
    body = text[6:]
    if "|" in body:
        title, year = body.rsplit("|", 1)
        try:
            return norm_title(title), int(year)
        except ValueError:
            return norm_title(body), None
    return norm_title(body), None


def compact_record(record, locator_tag=""):
    return {
        "dedup_key": record.get("dedup_key") or dedup_key(record),
        "title": record.get("title", ""),
        "doi": record.get("doi", ""),
        "pmid": record.get("pmid", ""),
        "arxiv_id": record.get("arxiv_id", ""),
        "ids": sorted(set(record.get("ids") or record_ids(record))),
        "keys": sorted(set(record.get("keys") or record_keys(record))),
        "authors": record.get("authors", ""),
        "journal": record.get("journal", ""),
        "year": record.get("year"),
        "index_date": iso(record.get("index_date")),
        "published_date": iso(record.get("published_date")),
        "version": record.get("version", ""),
        "retracted": bool(record.get("retracted")),
        "update_types": sorted(set(record.get("update_types") or [])),
        "is_preprint": bool(record.get("is_preprint")),
        "is_formal": bool(record.get("is_formal")),
        "locator": locator_tag,
    }


def snapshot_from_value(value, input_dir, locator):
    keys, records, watermark = [], [], None
    if value is None:
        return keys, records, watermark, locator
    if isinstance(value, str):
        candidate = Path(input_dir) / value
        loaded = None
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
        if loaded is not None:
            return snapshot_from_value(loaded, input_dir, locator + "->" + value)
        parsed = parse_dt(value)
        if parsed:
            watermark = value
            return keys, records, watermark, locator
        keys = [value]
        return keys, records, watermark, locator
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                keys.append(item)
            elif isinstance(item, dict):
                records.append(item)
        return keys, records, watermark, locator
    if isinstance(value, dict):
        for key in ("watermark", "last_seen", "waterline", "as_of", "cutoff",
                    "last_imported_at", "lastImportedAt", "imported_until"):
            if value.get(key) not in (None, ""):
                watermark = value[key]
                break
        for sub in ("keys", "seen_keys", "imported_ids", "ids", "seen", "history"):
            found = value.get(sub)
            if isinstance(found, list):
                keys.extend(item for item in found if isinstance(item, str))
        for sub in ("records", "items", "imported", "imported_records", "seen_records"):
            found = value.get(sub)
            if isinstance(found, list):
                records.extend(item for item in found if isinstance(item, dict))
        if not keys and not records:
            if all(isinstance(item, dict) for item in value.values()):
                records.extend(item for item in value.values() if isinstance(item, dict))
        return keys, records, watermark, locator
    return keys, records, watermark, locator


def read_snapshot(case, input_dir, errors):
    if not isinstance(case, dict):
        return {"keys": [], "records": [], "watermark": None, "locator": "",
                "present": False, "raw_value": None}
    found_key = None
    raw = None
    for key in ("previous_snapshot", "last_snapshot", "snapshot", "baseline_snapshot",
                "import_state", "state", "seen_snapshot"):
        value = case.get(key)
        if value not in (None, "", [], {}):
            found_key, raw = key, value
            break
    for key in ("snapshot_file", "previous_snapshot_path", "snapshot_path", "previous_snapshot_file"):
        if found_key is None and isinstance(case.get(key), str):
            found_key, raw = key, case[key]
    if found_key is None:
        for key in ("watermark", "last_imported_at", "since"):
            if case.get(key) not in (None, ""):
                raw = {"watermark": case[key], "keys": case.get("seen_keys") or []}
                found_key = key
                break
    if found_key is None:
        return {"keys": [], "records": [], "watermark": None, "locator": "",
                "present": False, "raw_value": None}
    locator = "/" + found_key
    keys, raw_records, watermark, locator = snapshot_from_value(raw, input_dir, locator)
    records = []
    for index, record in enumerate(raw_records):
        if isinstance(record, dict):
            normalized = normalize_record(record, {"file": "snapshot", "kind": "snapshot",
                                                   "pointer": locator + "/" + str(index)},
                                          "snapshot")
            normalized["keys"] = sorted(set(record_keys(normalized)))
            normalized["ids"] = sorted(set(record_ids(normalized)))
            normalized["dedup_key"] = dedup_key(normalized)
            records.append(compact_record(normalized, locator + "/" + str(index)))
    keys = unique([str(item) for item in keys])
    return {"keys": keys, "records": records, "watermark": watermark,
            "locator": locator, "present": True, "raw_value": raw}


def build_seen(snapshot):
    ids = set()
    keys = set(snapshot.get("keys") or [])
    title_pairs = []
    records = []
    id_map = {}
    for key in snapshot.get("keys") or []:
        for ident in snapshot_key_ids(key):
            ids.add(ident)
        pair = snapshot_title_pair(key)
        if pair:
            title_pairs.append(pair)
    for record in snapshot.get("records") or []:
        records.append(record)
        for ident in record.get("ids") or record_ids(record):
            ids.add(ident)
            id_map[ident] = record
        for key in record.get("keys") or []:
            keys.add(key)
            for ident in snapshot_key_ids(key):
                ids.add(ident)
                id_map.setdefault(ident, record)
        title = norm_title(record.get("title", ""))
        if title:
            title_pairs.append((title, record.get("year")))
    return {"ids": ids, "keys": keys, "title_pairs": title_pairs,
            "records": records, "id_map": id_map}


# ---------------------------------------------------------------------------
# window / subscription
# ---------------------------------------------------------------------------

def flatten_window(value):
    result = {"start": None, "end": None, "timezone": None, "locator": ""}
    if value is None:
        return result
    if isinstance(value, list) and len(value) >= 2:
        result["start"] = value[0]
        result["end"] = value[1]
        return result
    if isinstance(value, str):
        text = norm_space(value)
        parts = re.split(r"\s*(?:\.\.|—|–|~|to|至)\s*", text, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            result["start"], result["end"] = parts
        else:
            result["start"] = text
        return result
    if isinstance(value, dict):
        for key in ("start", "from", "start_date", "startDate", "since", "begin"):
            if value.get(key) not in (None, ""):
                result["start"] = value[key]
                break
        for key in ("end", "to", "end_date", "endDate", "until", "finish"):
            if value.get(key) not in (None, ""):
                result["end"] = value[key]
                break
        for key in ("timezone", "tz", "time_zone", "timeZone"):
            if value.get(key) not in (None, ""):
                result["timezone"] = value[key]
                break
    return result


def parse_window(case):
    candidates = []
    if isinstance(case, dict):
        for key in ("window", "time_window", "retrieval_window", "period", "date_range"):
            if case.get(key) not in (None, "", [], {}):
                candidates.append(("/" + key, case[key]))
        subscription = case.get("subscription")
        if isinstance(subscription, dict):
            for key in ("window", "time_window", "period"):
                if subscription.get(key) not in (None, "", [], {}):
                    candidates.append(("/subscription/" + key, subscription[key]))
        top = {}
        for key in ("window_start", "start_date", "since"):
            if case.get(key) not in (None, ""):
                top["start"] = case[key]
        for key in ("window_end", "end_date", "until"):
            if case.get(key) not in (None, ""):
                top["end"] = case[key]
        if top:
            candidates.append(("/", top))
    window = {"start": None, "end": None, "timezone": None, "locator": "",
              "raw_start": None, "raw_end": None}
    for locator, value in candidates:
        parsed = flatten_window(value)
        if parsed.get("start") or parsed.get("end"):
            window.update(parsed)
            window["locator"] = locator
            window["raw_start"] = parsed.get("start")
            window["raw_end"] = parsed.get("end")
            break
    if window["timezone"] is None and isinstance(case, dict):
        for key in ("timezone", "tz", "time_zone"):
            if case.get(key) not in (None, ""):
                window["timezone"] = case[key]
                break
    tzname = window["timezone"]
    start = parse_dt(window["start"]) if window["start"] is not None else None
    end = parse_dt(window["end"], end_of_day=True) if window["end"] is not None else None
    window["start_dt"] = to_naive(start, tzname)
    window["end_dt"] = to_naive(end, tzname)
    window["start_iso"] = iso(window["start_dt"])
    window["end_iso"] = iso(window["end_dt"])
    window["complete"] = bool(window["start_dt"] and window["end_dt"])
    window["end_inclusive"] = True
    window["raw_timezone"] = tzname
    window["timezone_label"] = tzname
    return window


def parse_subscription(case):
    raw = case.get("subscription") if isinstance(case, dict) else None
    if isinstance(raw, str):
        sub = {"query": norm_space(raw), "raw": raw}
    elif isinstance(raw, dict):
        sub = dict(raw)
        sub["raw"] = raw
    else:
        sub = {"raw": None}
    for key in ("name", "title", "topic", "query", "search_query", "searchQuery",
                "keywords", "sources", "source", "objectives", "description"):
        if key not in sub and isinstance(case, dict) and case.get(key) not in (None, ""):
            sub[key] = case[key]
    sub.setdefault("query", "")
    sub.setdefault("name", "")
    sources = sub.get("sources") or sub.get("source") or []
    if isinstance(sources, str):
        sources = [item.strip() for item in re.split(r"[,;]", sources) if item.strip()]
    if not isinstance(sources, list):
        sources = []
    sub["sources"] = [norm_space(item) for item in sources if norm_space(item)]
    rules = sub.get("alert_rules") or (case.get("alert_rules") if isinstance(case, dict) else None) or {}
    if not isinstance(rules, dict):
        rules = {}
    cap = POLICY.get("alert_caps", {})
    sub["alert_rules"] = {
        "max_items": int(rules.get("max_items", rules.get("max_alerts",
                                                          cap.get("max_items", 25)))),
        "include_updates": bool(rules.get("include_updates", cap.get("include_updates", True))),
        "include_retractions": bool(rules.get("include_retractions",
                                              cap.get("include_retractions", True))),
    }
    sub["query"] = norm_space(sub.get("query")) or ""
    sub["name"] = norm_space(sub.get("name")) or ""
    return sub


def query_from_task(text):
    if not text:
        return ""
    for pattern in (r"(?im)^\s*(?:query|search[_ ]query|检索式|查询式)\s*[:：]\s*(.+)$",
                    r"(?im)^\s*(?:keywords?)\s*[:：]\s*(.+)$"):
        match = re.search(pattern, text)
        if match:
            return norm_space(match.group(1))
    return ""


# ---------------------------------------------------------------------------
# dedup and version chains
# ---------------------------------------------------------------------------

def merge_unit(target, unit):
    for field in ("title", "authors", "journal", "doi", "pmid", "arxiv_id",
                  "version", "record_id", "source_label"):
        if not target.get(field) and unit.get(field):
            target[field] = unit[field]
    for field in ("markers", "update_types", "related"):
        target[field] = unique(list(target.get(field) or []) + list(unit.get(field) or []))
    target["retracted"] = bool(target.get("retracted") or unit.get("retracted"))
    target["is_preprint"] = bool(target.get("is_preprint") and unit.get("is_preprint"))
    target["is_formal"] = bool(target.get("is_formal") or unit.get("is_formal"))
    if unit.get("index_date") and (not target.get("index_date")
                                   or unit["index_date"] > target["index_date"]):
        target["index_date"] = unit["index_date"]
        target["index_field"] = unit.get("index_field")
        target["index_is_fallback"] = unit.get("index_is_fallback")
    if unit.get("published_date") and (not target.get("published_date")
                                       or unit["published_date"] > target["published_date"]):
        target["published_date"] = unit["published_date"]
        target["published_field"] = unit.get("published_field")
    if unit.get("year") and not target.get("year"):
        target["year"] = unit["year"]


def exact_merge(units):
    clusters = {}
    order = []
    for unit in units:
        key = dedup_key(unit)
        if key not in clusters:
            cluster = dict(unit)
            cluster["dedup_key"] = key
            cluster["members"] = [unit]
            cluster["ids"] = set(record_ids(unit))
            cluster["keys"] = set(record_keys(unit))
            cluster["locators"] = [unit.get("locator")]
            cluster["sources"] = [unit.get("provider")] if unit.get("provider") else []
            cluster["queries"] = [unit.get("query")] if unit.get("query") else []
            clusters[key] = cluster
            order.append(key)
        else:
            cluster = clusters[key]
            cluster["members"].append(unit)
            merge_unit(cluster, unit)
            cluster["ids"].update(record_ids(unit))
            cluster["keys"].update(record_keys(unit))
            cluster["locators"].append(unit.get("locator"))
            if unit.get("provider"):
                cluster["sources"].append(unit["provider"])
            if unit.get("query"):
                cluster["queries"].append(unit["query"])
    return [clusters[key] for key in order]


def author_tokens(text):
    parts = re.split(r"[;,]", text or "")
    tokens = set()
    for part in parts:
        words = re.findall(r"[a-z]{3,}", part.lower())
        if words:
            tokens.add(words[0])
    return tokens


def years_compatible(a, b):
    if a is None or b is None:
        return True
    return abs(int(a) - int(b)) <= 1


def should_link_titles(a, b):
    title = norm_title(a.get("title", ""))
    if len(title) < 20 or title != norm_title(b.get("title", "")):
        return False
    if not years_compatible(a.get("year"), b.get("year")):
        return False
    id_kinds_a = {item.split(":", 1)[0] for item in a.get("ids", set())}
    id_kinds_b = {item.split(":", 1)[0] for item in b.get("ids", set())}
    if ("arxiv" in id_kinds_a and "doi" in id_kinds_b) or \
            ("arxiv" in id_kinds_b and "doi" in id_kinds_a):
        return True
    tokens_a = author_tokens(a.get("authors", ""))
    tokens_b = author_tokens(b.get("authors", ""))
    if tokens_a and tokens_b:
        return bool(tokens_a & tokens_b)
    return False


def link_chains(clusters):
    parent = list(range(len(clusters)))
    reasons = {}

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b, reason):
        root_a, root_b = find(a), find(b)
        if root_a == root_b:
            return
        if root_a > root_b:
            root_a, root_b = root_b, root_a
        parent[root_b] = root_a
        reasons.setdefault(root_a, set()).add(reason)
        reasons.setdefault(root_b, set()).add(reason)

    id_index = {}
    for index, cluster in enumerate(clusters):
        for ident in cluster.get("ids", set()):
            id_index.setdefault(ident, []).append(index)
    for index, cluster in enumerate(clusters):
        for related in cluster.get("related") or []:
            ident = related.get("id")
            for other in id_index.get(ident, []):
                if other != index:
                    union(index, other, "identifier relation: " + str(related.get("relation")))

    title_groups = {}
    for index, cluster in enumerate(clusters):
        title = norm_title(cluster.get("title", ""))
        if len(title) >= 20:
            title_groups.setdefault(title, []).append(index)
    for indexes in title_groups.values():
        for position, left in enumerate(indexes):
            for right in indexes[position + 1:]:
                if should_link_titles(clusters[left], clusters[right]):
                    union(left, right, "same normalized title and compatible year/author")

    groups = {}
    for index, cluster in enumerate(clusters):
        groups.setdefault(find(index), []).append(cluster)

    chains = []
    for root, members in groups.items():
        chain = dict(members[0])
        chain["members"] = []
        for member in members:
            chain["members"].extend(member.get("members") or [])
        for member in members[1:]:
            merge_unit(chain, member)
        chain["ids"] = set()
        chain["keys"] = set()
        chain["locators"] = []
        chain["sources"] = []
        chain["queries"] = []
        for member in members:
            chain["ids"].update(member.get("ids", set()))
            chain["keys"].update(member.get("keys", set()))
            chain["locators"].extend(member.get("locators") or [])
            chain["sources"].extend(member.get("sources") or [])
            chain["queries"].extend(member.get("queries") or [])
        chain["sources"] = unique(chain["sources"])
        chain["queries"] = unique(chain["queries"])
        chain["chain_reasons"] = sorted(reasons.get(root, set()))
        formal = [member for member in members if member.get("is_formal") and member.get("doi")]
        with_doi = [member for member in members if member.get("doi")]
        representative = None
        for pool in (formal, with_doi, members):
            if pool:
                representative = sorted(pool, key=lambda item: (
                    -(item.get("published_date") or item.get("index_date")
                      or dt.datetime(1900, 1, 1)).timestamp(),
                    dedup_key(item)))[0]
                break
        chain["representative"] = representative
        chain["dedup_key"] = dedup_key(representative)
        chain["versions"] = []
        for member in sorted(members, key=lambda item: (
                item.get("published_date") or item.get("index_date") or dt.datetime(1900, 1, 1),
                dedup_key(item))):
            kind = "preprint" if member.get("is_preprint") and not member.get("doi") else "formal"
            label = member.get("version") or ("preprint" if kind == "preprint" else "published")
            chain["versions"].append({
                "label": label,
                "kind": kind,
                "venue": member.get("journal", ""),
                "date": iso(member.get("published_date") or member.get("index_date")),
                "provider": member.get("provider", ""),
                "ids": sorted(member.get("ids", set())),
                "locators": member.get("locators") or [],
                "retracted": bool(member.get("retracted")),
            })
        chains.append(chain)
    chains.sort(key=lambda item: (item.get("dedup_key", ""), len(item.get("members") or [])))
    return chains


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def chain_ids(chain):
    return set(chain.get("ids") or set())


def match_seen(chain, seen):
    for ident in sorted(chain_ids(chain)):
        record = seen["id_map"].get(ident)
        if record:
            return record, "identifier " + ident
        if ident in seen["ids"]:
            return {"title": chain.get("title", ""), "year": chain.get("year"),
                    "ids": [ident], "keys": [ident], "retracted": False,
                    "version": "", "update_types": []}, "snapshot key " + ident
    title = norm_title(chain.get("title", ""))
    if title:
        for pair_title, pair_year in seen["title_pairs"]:
            if pair_title and pair_title == title and years_compatible(chain.get("year"), pair_year):
                for record in seen["records"]:
                    if norm_title(record.get("title", "")) == title:
                        return record, "normalized title + year"
                return {"title": chain.get("title", ""), "year": pair_year,
                        "ids": [], "keys": [], "retracted": False, "version": "",
                        "update_types": []}, "normalized title + year"
    return None, ""


def version_changed(current, previous):
    cur_version = norm_space(current.get("version", ""))
    prev_version = norm_space((previous or {}).get("version", ""))
    if cur_version and cur_version != prev_version:
        return True, "version %s -> %s" % (prev_version or "(none)", cur_version)
    cur_types = set(current.get("update_types") or [])
    prev_types = set((previous or {}).get("update_types") or [])
    new_types = sorted(cur_types - prev_types)
    if new_types:
        return True, "new update marker: " + ", ".join(new_types)
    prev_ids = set((previous or {}).get("ids") or [])
    if current.get("doi") and not any(ident.startswith("doi:") for ident in prev_ids):
        return True, "formal DOI added: " + current["doi"]
    if current.get("is_preprint") and current.get("is_formal"):
        if not any(ident.startswith("doi:") for ident in prev_ids):
            return True, "preprint promoted to a formal publication"
    return False, ""


def item_evidence(chain, condition):
    entries = []
    for member in (chain.get("members") or [])[:MAX_EVIDENCE_PER_ITEM]:
        locator = member.get("locator") or {}
        entry = {
            "kind": "item",
            "file": locator.get("file", member.get("file", "")),
            "provider": member.get("provider", ""),
            "query": member.get("query", ""),
            "condition": condition,
        }
        for key in ("pointer", "line", "row", "xpath"):
            if locator.get(key) not in (None, ""):
                entry[key] = locator[key]
        if "pointer" not in entry and "line" not in entry and "row" not in entry \
                and "xpath" not in entry:
            entry["locator"] = str(locator)
        entries.append(entry)
    return entries


def item_payload(chain, reason, extra=None):
    representative = chain.get("representative") or chain
    payload = {
        "dedup_key": chain.get("dedup_key"),
        "record_id": representative.get("record_id", ""),
        "title": chain.get("title", ""),
        "doi": chain.get("doi") or None,
        "doi_list": sorted({member.get("doi") for member in chain.get("members") or []
                            if member.get("doi")}),
        "pmid": chain.get("pmid") or None,
        "pmid_list": sorted({member.get("pmid") for member in chain.get("members") or []
                             if member.get("pmid")}),
        "arxiv_id": chain.get("arxiv_id") or None,
        "arxiv_list": sorted({member.get("arxiv_id") for member in chain.get("members") or []
                              if member.get("arxiv_id")}),
        "authors": chain.get("authors", ""),
        "journal": chain.get("journal", ""),
        "published": iso(chain.get("published_date")),
        "indexed": iso(chain.get("index_date")),
        "year": chain.get("year"),
        "version": chain.get("version") or None,
        "versions": chain.get("versions") or [],
        "sources": chain.get("sources") or [],
        "source_locators": chain.get("locators") or [],
        "evidence": item_evidence(chain, reason),
        "reason": reason,
    }
    if extra:
        payload.update(extra)
    return payload


def classify(chains, seen, window):
    new_items, updates, retractions, already_seen, out_of_window, late_indexed = \
        [], [], [], [], [], []
    start, end = window.get("start_dt"), window.get("end_dt")
    for chain in chains:
        index_date = chain.get("index_date")
        published_date = chain.get("published_date")
        event_date = index_date or published_date
        in_window = True
        window_reason = ""
        late = False
        if event_date is not None:
            if start and event_date < start:
                if index_date is None or chain.get("index_is_fallback") or published_date and published_date < start:
                    late = True
                    window_reason = ("publication date %s is before window start %s; "
                                     "no separate index timestamp"
                                     % (iso(published_date), iso(start)))
                else:
                    in_window = False
                    window_reason = "index date %s is before window start %s" % (iso(event_date), iso(start))
            elif end and event_date > end:
                in_window = False
                window_reason = "index date %s is after window end %s" % (iso(event_date), iso(end))
        date_missing = event_date is None
        if (not late and in_window and start and published_date and index_date
                and published_date < start and index_date >= start):
            coarse_year = (chain.get("published_precision") == "year"
                           and published_date.year == start.year)
            if not coarse_year:
                late = True
                window_reason = ("publication date %s is before window start %s; "
                                 "the record was only indexed at %s"
                                 % (iso(published_date), iso(start), iso(index_date)))
        matched, match_reason = match_seen(chain, seen)
        retracted = bool(chain.get("retracted"))
        if not in_window:
            out_of_window.append(item_payload(chain, window_reason or "outside the declared window"))
            continue
        if retracted and not (matched and matched.get("retracted")):
            extra = {"retraction": {"detected": True,
                                    "type": (chain.get("update_types") or ["retraction"])[0],
                                    "matched_previous": bool(matched),
                                    "match_reason": match_reason}}
            retractions.append(item_payload(chain,
                                            "retraction notice in this window", extra))
            continue
        if matched:
            if retracted and matched.get("retracted"):
                already_seen.append(item_payload(
                    chain, "retraction already reported in the previous snapshot",
                    {"match_reason": match_reason}))
                continue
            changed, detail = version_changed(chain, matched)
            if changed:
                updates.append(item_payload(
                    chain, detail,
                    {"match_reason": match_reason,
                     "previous_version": matched.get("version", ""),
                     "previous_update_types": matched.get("update_types", [])}))
            else:
                already_seen.append(item_payload(
                    chain, "already present in the previous snapshot",
                    {"match_reason": match_reason}))
            continue
        if late:
            late_indexed.append(item_payload(chain, window_reason, {"late_indexed": True}))
            continue
        if date_missing:
            new_items.append(item_payload(chain, "not present in the previous snapshot; date missing"))
        else:
            new_items.append(item_payload(chain, "not present in the previous snapshot"))
    return {
        "new_items": new_items,
        "updates": updates,
        "retractions": retractions,
        "already_seen": already_seen,
        "out_of_window": out_of_window,
        "late_indexed": late_indexed,
    }


def duplicate_groups(clusters):
    groups = []
    for cluster in clusters:
        members = cluster.get("members") or []
        if len(members) <= 1:
            continue
        ids = cluster.get("ids") or set()
        if any(ident.startswith("doi:") for ident in ids):
            reason = "same DOI after normalization"
        elif any(ident.startswith("pmid:") for ident in ids):
            reason = "same PMID after normalization"
        elif any(ident.startswith("arxiv:") for ident in ids):
            reason = "same arXiv identifier after normalization"
        else:
            reason = "same normalized title + year"
        groups.append({
            "dedup_key": cluster.get("dedup_key"),
            "title": cluster.get("title", ""),
            "raw_count": len(members),
            "locators": cluster.get("locators") or [],
            "reason": reason,
        })
    groups.sort(key=lambda item: (item["dedup_key"] or "", item["title"]))
    return groups


def apply_alert_rules(categories, rules, duplicate_count):
    max_items = int(rules.get("max_items", 25))
    include_updates = bool(rules.get("include_updates", True))
    include_retractions = bool(rules.get("include_retractions", True))
    alerts, suppressed = [], []

    def add(item, kind, priority):
        alert = dict(item)
        alert["kind"] = kind
        alert["priority"] = priority
        alerts.append(alert)

    if include_retractions:
        for item in categories["retractions"]:
            add(item, "retraction", "high")
    else:
        for item in categories["retractions"]:
            suppressed.append({"dedup_key": item.get("dedup_key"), "title": item.get("title"),
                               "kind": "retraction",
                               "reason": "retractions disabled by alert rules"})
    if include_updates:
        for item in categories["updates"]:
            add(item, "version_update", "medium")
    else:
        for item in categories["updates"]:
            suppressed.append({"dedup_key": item.get("dedup_key"), "title": item.get("title"),
                               "kind": "version_update",
                               "reason": "version updates disabled by alert rules"})
    for item in categories["new_items"]:
        add(item, "new_item", "normal")

    for item in categories["late_indexed"]:
        suppressed.append({"dedup_key": item.get("dedup_key"), "title": item.get("title"),
                           "kind": "late_indexed",
                           "reason": "published before the window but newly indexed; "
                                     "suppressed to avoid presenting old literature as new"})
    for item in categories["already_seen"]:
        suppressed.append({"dedup_key": item.get("dedup_key"), "title": item.get("title"),
                           "kind": "already_seen",
                           "reason": "already delivered in the previous snapshot"})
    for item in categories["out_of_window"]:
        suppressed.append({"dedup_key": item.get("dedup_key"), "title": item.get("title"),
                           "kind": "out_of_window",
                           "reason": item.get("reason", "outside the declared window")})
    for group in duplicate_count:
        suppressed.append({"dedup_key": group.get("dedup_key"), "title": group.get("title"),
                           "kind": "duplicate",
                           "reason": "duplicate merged: " + group.get("reason", "")})

    priority_rank = {"high": 0, "medium": 1, "normal": 2}
    alerts.sort(key=lambda item: (priority_rank.get(item.get("priority"), 9),
                                  norm_title(item.get("title", "")),
                                  item.get("dedup_key") or ""))
    kept = alerts[:max_items]
    for item in alerts[max_items:]:
        suppressed.append({"dedup_key": item.get("dedup_key"), "title": item.get("title"),
                           "kind": item.get("kind"),
                           "reason": "alert budget reached (max_items=%d)" % max_items})
    if len(suppressed) > MAX_SUPPRESSED:
        suppressed = suppressed[:MAX_SUPPRESSED]
    return kept, suppressed


# ---------------------------------------------------------------------------
# output contract
# ---------------------------------------------------------------------------
ALIAS_TABLE = {
    "case_id": "case_id", "caseid": "case_id", "case": "case_id",
    "status": "status", "run_status": "status",
    "subscription": "subscription", "config": "subscription", "configuration": "subscription",
    "query_config": "subscription", "subscription_config": "subscription",
    "window": "window", "time_window": "window", "period": "window",
    "window_range": "window", "date_range": "window",
    "new_items": "new_items", "new_papers": "new_items", "records": "new_items",
    "items": "new_items", "papers": "new_items", "candidates": "new_items",
    "candidate_items": "new_items", "delta": "new_items", "increment": "new_items",
    "new_findings": "new_items", "findings": "new_items", "results": "new_items",
    "retrieved": "new_items", "new_records": "new_items",
    "updates": "updates", "changes": "updates", "version_updates": "updates",
    "revisions": "updates", "change_items": "updates", "updated_items": "updates",
    "retractions": "retractions", "withdrawn": "retractions", "withdrawals": "retractions",
    "retracted": "retractions", "retraction_notices": "retractions",
    "already_seen": "already_seen", "seen": "already_seen", "history": "already_seen",
    "previous_items": "already_seen", "known_items": "already_seen",
    "out_of_window": "out_of_window", "missed": "out_of_window", "omitted": "out_of_window",
    "outside_window": "out_of_window", "late": "out_of_window",
    "late_indexed": "late_indexed", "delayed_indexed": "late_indexed",
    "duplicates": "duplicates", "dupes": "duplicates", "duplicate_items": "duplicates",
    "dedup": "duplicates",
    "alerts": "alerts", "notifications": "alerts", "alert_list": "alerts",
    "digest": "alerts", "reminders": "alerts",
    "suppressed": "suppressed", "suppressions": "suppressed", "muted": "suppressed",
    "evidence": "evidence", "refs": "evidence", "references": "evidence",
    "sources": "evidence", "citations": "evidence", "provenance": "evidence",
    "source_map": "evidence", "evidence_records": "evidence", "trace": "evidence",
    "snapshot": "snapshot", "state": "snapshot", "next_snapshot": "next_snapshot",
    "next_state": "next_snapshot",
    "uncertainty": "uncertainty", "uncertainties": "uncertainty", "notes": "uncertainty",
    "limitations": "uncertainty", "open_questions": "uncertainty", "caveats": "uncertainty",
    "run_log": "run_log", "log": "run_log", "change_log": "run_log",
    "execution_log": "run_log", "trace_log": "run_log",
    "summary": "summary", "overview": "summary", "abstract": "summary", "brief": "summary",
    "stats": "stats", "metrics": "stats", "counts": "stats", "statistics": "stats",
    "replay": "replay", "replay_info": "replay",
    "result": "result", "payload": "result", "data": "result",
}

DIRECT_KEYS = {
    "case_id", "status", "subscription", "window", "new_items", "updates",
    "retractions", "already_seen", "out_of_window", "late_indexed", "duplicates",
    "alerts", "suppressed", "evidence", "snapshot", "next_snapshot", "uncertainty",
    "run_log", "summary", "stats", "replay", "result", "errors", "contract",
    "contract_extras", "status_reason",
}


def extract_contract(case):
    if not isinstance(case, dict):
        return {}
    raw = case.get("output_contract")
    if isinstance(raw, dict):
        return {str(key): value for key, value in raw.items()}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, str):
                out[item] = ""
            elif isinstance(item, dict):
                name = item.get("name") or item.get("key") or item.get("field") or item.get("id")
                if name:
                    out[str(name)] = item.get("description") or item.get("type") or ""
        return out
    return {}


def normalize_contract_key(key):
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def best_effort_value(key, payload):
    low = normalize_contract_key(key)
    if any(word in low for word in ("summary", "overview", "abstract", "brief")):
        return payload.get("summary", ""), "summary string"
    if "status" in low:
        return payload.get("status", ""), "status string"
    if any(word in low for word in ("count", "number", "total", "num")):
        return 0, "honest zero count"
    if any(word in low for word in ("note", "uncertainty", "limitation", "caveat")):
        return payload.get("uncertainty", []), "uncertainty list"
    return [], "no matching producer; empty list with an explanation in uncertainty"


def allowed_status_values(spec):
    text = spec if isinstance(spec, str) else json.dumps(spec, ensure_ascii=False)
    tokens = set(re.findall(r"[a-z_]+", text.lower()))
    return tokens & {"success", "partial", "insufficient_input", "failed", "completed",
                     "answered", "insufficient_evidence", "input_error", "ok", "error"}


def status_for_contract(status, spec):
    allowed = allowed_status_values(spec)
    if not allowed or status in allowed:
        return status
    preferences = {
        "success": ("success", "completed", "answered", "ok"),
        "partial": ("partial", "insufficient_evidence", "incomplete"),
        "insufficient_input": ("insufficient_input", "insufficient_evidence", "input_error"),
        "failed": ("failed", "error", "input_error"),
    }
    for candidate in preferences.get(status, ()):
        if candidate in allowed:
            return candidate
    return status


def apply_contract(payload, case):
    contract = extract_contract(case)
    extras = {}
    mapped = {}
    for key, spec in contract.items():
        if key in ("output_contract",) or not str(key).strip():
            continue
        if key in payload and payload.get(key) is not None:
            value = payload[key]
            mapped[key] = "direct"
        else:
            target = ALIAS_TABLE.get(normalize_contract_key(key))
            if target and target in payload and payload.get(target) is not None:
                value = payload[target]
                mapped[key] = "alias->" + target
            else:
                value, note = best_effort_value(key, payload)
                mapped[key] = "best_effort"
                extras[key] = {"source": "best_effort", "note": note,
                               "value_type": type(value).__name__}
                payload.setdefault("uncertainty", []).append({
                    "type": "contract_key_not_produced",
                    "key": key,
                    "detail": "output_contract declares '%s' but no producer matched it; "
                              "an honest empty value was emitted." % key,
                    "resolution": "reviewer or executing agent should fill it from evidence",
                })
        if key == "status":
            value = status_for_contract(payload.get("status", "partial"), spec)
        payload[key] = value
    if contract:
        payload["contract"] = {
            "declared_keys": sorted(contract.keys()),
            "mapped": mapped,
            "extras": extras,
            "all_present": all(key in payload for key in contract),
        }
    else:
        payload["contract"] = {
            "declared_keys": [],
            "mapped": {},
            "extras": {},
            "all_present": True,
        }
    payload["contract_extras"] = extras
    return payload


# ---------------------------------------------------------------------------
# next snapshot and result assembly
# ---------------------------------------------------------------------------

def build_next_snapshot(chains, snapshot, window, seen):
    records = {}
    order = []
    key_index = {}

    def register(record_key, record):
        for candidate in set([record.get("dedup_key")] + list(record.get("keys") or [])
                             + list(record.get("ids") or [])):
            if candidate:
                key_index.setdefault(str(candidate), record_key)

    def merge_into(target_key, target, record):
        target["ids"] = sorted(set(target.get("ids") or []) | set(record.get("ids") or []))
        target["keys"] = sorted(set(target.get("keys") or []) | set(record.get("keys") or []))
        target["retracted"] = bool(target.get("retracted") or record.get("retracted"))
        if record.get("version") and not target.get("version"):
            target["version"] = record["version"]
        target["update_types"] = sorted(set(target.get("update_types") or [])
                                        | set(record.get("update_types") or []))
        if record.get("index_date") and (not target.get("index_date")
                                         or record["index_date"] > target["index_date"]):
            target["index_date"] = record["index_date"]
        if record.get("published_date") and (not target.get("published_date")
                                             or record["published_date"] > target["published_date"]):
            target["published_date"] = record["published_date"]
        for field in ("doi", "pmid", "arxiv_id", "authors", "journal"):
            if not target.get(field) and record.get(field):
                target[field] = record[field]
        register(target_key, target)

    def add_record(record):
        candidates = [record.get("dedup_key")] + list(record.get("keys") or []) \
            + list(record.get("ids") or [])
        matched = None
        for candidate in candidates:
            if candidate and str(candidate) in key_index:
                matched = key_index[str(candidate)]
                break
        if matched is not None:
            merge_into(matched, records[matched], record)
            return
        key = record.get("dedup_key") or (record.get("ids") or [""])[0] or record.get("title")
        if not key:
            return
        records[key] = record
        order.append(key)
        register(key, record)

    for record in snapshot.get("records") or []:
        add_record(dict(record))
    for chain in chains:
        compact = compact_record({
            "dedup_key": chain.get("dedup_key"),
            "title": chain.get("title", ""),
            "doi": chain.get("doi", ""),
            "pmid": chain.get("pmid", ""),
            "arxiv_id": chain.get("arxiv_id", ""),
            "ids": sorted(chain_ids(chain)),
            "keys": sorted(chain.get("keys") or set()),
            "authors": chain.get("authors", ""),
            "journal": chain.get("journal", ""),
            "year": chain.get("year"),
            "index_date": chain.get("index_date"),
            "published_date": chain.get("published_date"),
            "version": chain.get("version", ""),
            "retracted": chain.get("retracted"),
            "update_types": chain.get("update_types"),
            "is_preprint": chain.get("is_preprint"),
            "is_formal": chain.get("is_formal"),
        })
        add_record(compact)
    keys = set(snapshot.get("keys") or [])
    for record in snapshot.get("records") or []:
        keys.update(record.get("keys") or [])
    for chain in chains:
        keys.update(chain.get("keys") or set())
    watermark = window.get("end_iso") or snapshot.get("watermark")
    if not watermark:
        dates = [chain.get("index_date") for chain in chains if chain.get("index_date")]
        watermark = iso(max(dates)) if dates else None
    snapshot_records = [records[key] for key in order][:MAX_SNAPSHOT_RECORDS]
    return {
        "watermark": watermark,
        "keys": sorted(str(key) for key in keys),
        "records": snapshot_records,
        "window": {"start": window.get("start_iso"), "end": window.get("end_iso"),
                   "timezone": window.get("timezone")},
        "generated_by": "scripts/sk_entry.py",
        "skill_version": SKILL_VERSION,
    }


def build_summary(categories, alerts, suppressed, snapshot, window):
    start = window.get("start_iso") or "?"
    end = window.get("end_iso") or "?"
    tz = window.get("timezone") or "unknown"
    return (
        "窗口 %s..%s（时区 %s）：新增 %d 条、版本更新 %d 条、撤稿 %d 条、"
        "已见 %d 条、窗外 %d 条、延迟入库 %d 条、重复合并 %d 组；"
        "提醒 %d 条、抑制 %d 条；快照键 %d → %d（watermark=%s）。"
        % (start, end, tz, len(categories["new_items"]), len(categories["updates"]),
           len(categories["retractions"]), len(categories["already_seen"]),
           len(categories["out_of_window"]), len(categories["late_indexed"]),
           len(categories["duplicates"]), len(alerts), len(suppressed),
           snapshot.get("previous_keys", 0), snapshot.get("next_keys", 0),
           snapshot.get("watermark") or "none")
    )


def global_evidence(window, subscription, snapshot, files, chains, categories,
                    duplicates, errors):
    evidence = []
    evidence.append({
        "kind": "window",
        "start": window.get("start_iso"),
        "end": window.get("end_iso"),
        "timezone": window.get("timezone"),
        "locator": window.get("locator"),
        "complete": window.get("complete"),
    })
    evidence.append({
        "kind": "subscription",
        "name": subscription.get("name"),
        "query": subscription.get("query"),
        "sources": subscription.get("sources"),
        "alert_rules": subscription.get("alert_rules"),
    })
    evidence.append({
        "kind": "snapshot",
        "locator": snapshot.get("locator"),
        "previous_keys": len(snapshot.get("keys") or []),
        "previous_records": len(snapshot.get("records") or []),
        "watermark": snapshot.get("watermark"),
    })
    for item in files:
        evidence.append({
            "kind": "source_file",
            "file": item.get("rel"),
            "provider": item.get("provider"),
            "bytes": item.get("size"),
            "sha256": item.get("sha256"),
            "records": item.get("records"),
            "parse_status": item.get("status"),
            "query": item.get("query"),
        })
    for category in ("new_items", "updates", "retractions", "already_seen",
                     "out_of_window", "late_indexed"):
        for item in categories.get(category, [])[:MAX_OUTPUT_ITEMS]:
            evidence.append({
                "kind": category,
                "dedup_key": item.get("dedup_key"),
                "title": item.get("title"),
                "reason": item.get("reason"),
                "locators": item.get("evidence") or [],
            })
    for group in duplicates:
        evidence.append({
            "kind": "duplicate_group",
            "dedup_key": group.get("dedup_key"),
            "title": group.get("title"),
            "raw_count": group.get("raw_count"),
            "locators": group.get("locators"),
            "reason": group.get("reason"),
        })
    for error in errors:
        evidence.append({"kind": "parse_error", **error})
    return evidence


def uncertainty_list(case, case_error, window, subscription, snapshot, records,
                     files, errors, duplicates, chains, late_indexed, categories,
                     contract):
    out = []
    if case_error:
        out.append({"type": "case_parse_error", "detail": case_error.get("message", ""),
                    "file": case_error.get("file", "")})
    if not case:
        if case_error:
            out.append({"type": "case_unreadable",
                        "detail": "A case file was found but could not be parsed; no "
                                  "subscription or window could be read.",
                        "resolution": "the fixed fields are still written, with empty lists"})
        else:
            out.append({"type": "no_case_file",
                        "detail": "No readable case.json was found; no subscription or "
                                  "window could be read.",
                        "resolution": "the fixed fields are still written, with empty lists"})
    if not window.get("complete"):
        out.append({"type": "window_incomplete",
                    "detail": "Window start and/or end could not be parsed; in-window "
                              "filtering is therefore not enforced.",
                    "resolution": "records are still classified against the snapshot"})
    if not window.get("timezone"):
        out.append({"type": "timezone_missing",
                    "detail": "No timezone was declared; naive timestamps were compared "
                              "as supplied and are echoed as null.",
                    "resolution": "declare window.timezone to make the comparison explicit"})
    if not snapshot.get("present"):
        out.append({"type": "no_previous_snapshot",
                    "detail": "No previous snapshot or watermark was supplied, so this "
                              "run is a baseline and every unseen in-window item is new.",
                    "resolution": "feed back next_snapshot as previous_snapshot for the next window"})
    if not records:
        out.append({"type": "no_records_found",
                    "detail": "No record-like objects were found in case.json or the "
                              "supported source files.",
                    "resolution": "check the input file extensions and nesting; empty lists "
                                  "are honest and no records are fabricated"})
    if not subscription.get("query"):
        out.append({"type": "query_missing",
                    "detail": "No subscription query was found; retrieval provenance is "
                              "limited to the source files actually present."})
    missing_dates = [chain.get("dedup_key") for chain in chains
                     if not chain.get("index_date") and not chain.get("published_date")]
    if missing_dates:
        out.append({"type": "missing_dates",
                    "detail": "%d record(s) have neither an index nor a publication date; "
                              "they are kept but not placed in the window." % len(missing_dates),
                    "sample_dedup_keys": missing_dates[:10]})
    if any(chain.get("index_is_fallback") for chain in chains):
        out.append({"type": "index_date_from_publication",
                    "detail": "Some records have no index/import timestamp; their "
                              "publication date was used as the event date and marked."})
    if late_indexed:
        out.append({"type": "late_indexed_present",
                    "detail": "%d record(s) were published before the window but indexed "
                              "inside it; they are suppressed from alerts." % len(late_indexed)})
    if duplicates:
        out.append({"type": "duplicates_merged",
                    "detail": "%d duplicate group(s) were merged; all source locators "
                              "are retained in evidence." % len(duplicates)})
    if errors:
        out.append({"type": "parse_errors",
                    "detail": "%d file/line parsing error(s) were recorded in errors; "
                              "affected records were skipped rather than invented."
                              % len(errors)})
    skipped_refs = [item.get("rel") for item in files
                    if item.get("reference_like")]
    if skipped_refs:
        out.append({"type": "reference_graph_not_treated_as_retrieval",
                    "detail": "Reference/citation graph files were read but not counted as "
                              "retrieval results: " + ", ".join(skipped_refs[:10]),
                    "resolution": "use them as relevance context, not as new-paper evidence"})
    pdfs = [item.get("rel") for item in files if item.get("pdf")]
    if pdfs:
        out.append({"type": "pdf_not_parsed",
                    "detail": "PDF material was present but this stdlib-only skill does "
                              "not parse PDFs: " + ", ".join(pdfs[:10]),
                    "resolution": "records must come from structured source dumps; the PDF "
                                  "is left for the executing agent to read"})
    declared = [str(item).lower() for item in subscription.get("sources") or []]
    observed = {str(chain.get("sources") or []) for chain in chains}
    observed_flat = " ".join(sorted(observed)).lower()
    missing_sources = [item for item in declared if item and item not in observed_flat]
    if missing_sources:
        out.append({"type": "source_not_observed",
                    "detail": "Declared source(s) with no observed records: "
                              + ", ".join(missing_sources[:10])})
    if chains:
        out.append({"type": "title_match_residual_risk",
                    "detail": "Title-based dedup/snapshot matching can still merge two "
                              "different works with identical normalized titles; "
                              "identifier matches take priority."})
    if contract:
        out.append({"type": "contract_alias_note",
                    "detail": "Runtime output_contract keys were mapped by name/alias; "
                              "see contract.mapped and contract_extras."})
    out.append({"type": "no_live_network",
               "detail": "No live source was queried; only the supplied files were used."})
    return out


def result_status(case, records, window, snapshot, errors, categories):
    if not case and not records:
        return "insufficient_input", "No case descriptor and no records were readable."
    if not records:
        if window.get("complete") and snapshot.get("present"):
            return "success", "The window is empty: no records were supplied, which is a valid no-change result."
        if window.get("complete") or snapshot.get("present"):
            return "partial", "No records were found; window and snapshot are only partially available."
        return "insufficient_input", "No records were found and neither a complete window nor a snapshot is available."
    if not window.get("complete") or not snapshot.get("present"):
        missing = []
        if not window.get("complete"):
            missing.append("window")
        if not snapshot.get("present"):
            missing.append("previous_snapshot")
        return "partial", "Records were processed but %s is missing." % " and ".join(missing)
    if errors and not categories["new_items"] and not categories["updates"] \
            and not categories["retractions"] and not categories["already_seen"]:
        return "partial", "Parsing errors occurred and no record could be classified."
    return "success", "Subscription window, previous snapshot and source records were all available."


def build_payload(case, case_path, input_dir, case_error, task_text,
                  excluded_roots=None):
    errors = []
    if case_error:
        errors.append(case_error)
    window = parse_window(case)
    subscription = parse_subscription(case)
    if not subscription.get("query"):
        task_query = query_from_task(task_text)
        if task_query:
            subscription["query"] = task_query
            subscription["query_source"] = "task.md"
    snapshot = read_snapshot(case, input_dir, errors)
    seen = build_seen(snapshot)

    raw_units = []
    skipped_refs = set()
    files_info = []
    case_inline_units = []
    if case:
        out, skipped = [], set()
        ctx = {"file": "case.json", "provider": "case", "query": subscription.get("query", "")}
        walk_json(case, ctx, out, skipped, allow_record=False)
        skip_paths = {"/" + key for key in NON_RECORD_KEYS}
        for raw, _, loc in out:
            if loc.get("pointer") in skip_paths:
                continue
            case_inline_units.append((raw, loc, "case"))
        skipped_refs.update(skipped)
        if out:
            files_info.append({"rel": "case.json", "provider": "case",
                               "size": 0, "sha256": "", "records": len(case_inline_units),
                               "status": "parsed", "query": subscription.get("query", ""),
                               "reference_like": False, "pdf": False})

    file_entries, _total = list_input_files(input_dir, excluded_roots)
    for entry in file_entries:
        path = entry["path"]
        rel = str(path.relative_to(input_dir)) if path != input_dir else path.name
        if entry.get("too_large") or entry.get("over_budget"):
            files_info.append({"rel": rel, "provider": infer_provider(rel), "size": entry["size"],
                               "sha256": "", "records": 0,
                               "status": "skipped_too_large" if entry.get("too_large") else "skipped_over_budget",
                               "query": "", "reference_like": False, "pdf": False})
            continue
        if path.suffix.lower() == ".pdf":
            files_info.append({"rel": rel, "provider": infer_provider(rel),
                               "size": entry["size"], "sha256": sha256_file(path),
                               "records": 0, "status": "skipped_pdf", "query": "",
                               "reference_like": False, "pdf": True})
            continue
        if REFERENCE_LIKE_FILES.search(rel):
            files_info.append({"rel": rel, "provider": infer_provider(rel), "size": entry["size"],
                               "sha256": sha256_file(path), "records": 0,
                               "status": "skipped_reference_graph", "query": "",
                               "reference_like": True, "pdf": False})
            skipped_refs.add(rel)
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            errors.append({"stage": "read_file", "file": rel, "type": type(exc).__name__,
                           "message": str(exc)[:200]})
            files_info.append({"rel": rel, "provider": infer_provider(rel), "size": entry["size"],
                               "sha256": "", "records": 0, "status": "unreadable",
                               "query": "", "reference_like": False, "pdf": False})
            continue
        provider = infer_provider(rel)
        suffix = path.suffix.lower()
        try:
            if suffix == ".json":
                data, local, skipped = parse_json_text(text, rel, provider)
                provider = infer_provider(rel, data)
                raw_units.extend(local)
                skipped_refs.update(skipped)
            elif suffix in (".jsonl", ".ndjson"):
                local, skipped = parse_jsonl_text(text, rel, provider, errors)
                raw_units.extend(local)
                skipped_refs.update(skipped)
            elif suffix in (".csv", ".tsv"):
                local, skipped = parse_csv_text(text, rel, provider, errors,
                                                delimiter="\t" if suffix == ".tsv" else None)
                raw_units.extend(local)
                skipped_refs.update(skipped)
            elif suffix == ".xml":
                local, skipped = parse_xml_text(text, rel, provider, errors)
                raw_units.extend(local)
                skipped_refs.update(skipped)
            else:
                stripped = text.lstrip()
                if stripped.startswith("{") or stripped.startswith("["):
                    try:
                        data, local, skipped = parse_json_text(text, rel, provider)
                        provider = infer_provider(rel, data)
                        raw_units.extend(local)
                        skipped_refs.update(skipped)
                    except ValueError:
                        local, skipped = parse_jsonl_text(text, rel, provider, errors)
                        raw_units.extend(local)
                        skipped_refs.update(skipped)
                elif all(_line_is_json(line) for line in text.splitlines() if line.strip()):
                    local, skipped = parse_jsonl_text(text, rel, provider, errors)
                    raw_units.extend(local)
                    skipped_refs.update(skipped)
                elif _looks_tabular(text):
                    local, skipped = parse_csv_text(text, rel, provider, errors)
                    raw_units.extend(local)
                    skipped_refs.update(skipped)
                else:
                    files_info.append({"rel": rel, "provider": provider, "size": entry["size"],
                                       "sha256": sha256_file(path), "records": 0,
                                       "status": "unstructured_text", "query": "",
                                       "reference_like": False, "pdf": False})
                    continue
        except (OSError, ValueError) as exc:
            errors.append({"stage": "parse_file", "file": rel, "type": type(exc).__name__,
                           "message": str(exc)[:200]})
            files_info.append({"rel": rel, "provider": provider, "size": entry["size"],
                               "sha256": sha256_file(path), "records": 0,
                               "status": "parse_error", "query": "",
                               "reference_like": False, "pdf": False})
            continue
        local_count = len([item for item in raw_units
                           if item[2].get("file") == rel])
        files_info.append({"rel": rel, "provider": provider, "size": entry["size"],
                           "sha256": sha256_file(path), "records": local_count,
                           "status": "parsed", "query": "",
                           "reference_like": False, "pdf": False})

    units = []
    for raw, ctx, loc in case_inline_units:
        locator = dict(loc)
        locator["file"] = "case.json"
        units.append(normalize_record(raw, locator, "case", subscription.get("query", ""),
                                      case_inline=True))
    for raw, ctx, loc in raw_units:
        locator = dict(loc)
        locator["file"] = loc.get("file") or ""
        units.append(normalize_record(raw, locator, ctx.get("provider", ""),
                                      ctx.get("query") or subscription.get("query", ""),
                                      case_inline=False))
    units = [unit for unit in units if unit.get("title") or unit.get("doi")
             or unit.get("pmid") or unit.get("arxiv_id")]
    for unit in units:
        unit["index_date"] = to_naive(unit.get("index_date"), window.get("timezone"))
        unit["published_date"] = to_naive(unit.get("published_date"), window.get("timezone"))
    if len(units) > MAX_RECORDS:
        units = units[:MAX_RECORDS]
        errors.append({"stage": "limit_records", "type": "Truncated",
                       "message": "records limited to %d" % MAX_RECORDS})

    clusters = exact_merge(units)
    chains = link_chains(clusters)
    categories = classify(chains, seen, window)
    categories["duplicates"] = duplicate_groups(clusters)
    alerts, suppressed = apply_alert_rules(categories, subscription.get("alert_rules", {}),
                                           categories["duplicates"])
    next_snapshot = build_next_snapshot(chains, snapshot, window, seen)

    previous_keys = len(set(snapshot.get("keys") or []) | {key for record in snapshot.get("records") or []
                                                           for key in record.get("keys") or []})
    current_keys = set()
    for chain in chains:
        current_keys.update(chain.get("keys") or set())
    next_keys = len(next_snapshot.get("keys") or [])
    snapshot_summary = {
        "previous_keys": previous_keys,
        "current_keys": len(current_keys),
        "next_keys": next_keys,
        "watermark": next_snapshot.get("watermark"),
        "locator": snapshot.get("locator"),
        "present": snapshot.get("present"),
        "advanced": bool(next_keys > previous_keys or
                         (next_snapshot.get("watermark") and
                          next_snapshot.get("watermark") != snapshot.get("watermark"))),
        "key_definition": POLICY.get("dedup_priority", []),
        "records": len(next_snapshot.get("records") or []),
        "keys": sorted(next_snapshot.get("keys") or []),
    }

    contract = extract_contract(case)
    uncertainties = uncertainty_list(case, case_error, window, subscription, snapshot,
                                     units, files_info, errors, categories["duplicates"],
                                     chains, categories["late_indexed"], categories, contract)
    for rel in sorted(skipped_refs):
        uncertainties.append({"type": "reference_like_input_ignored",
                              "detail": "Reference/citation-like path was not used as a "
                                        "retrieval result: " + str(rel)})
    status, status_reason = result_status(case, units, window, snapshot, errors, categories)
    summary = build_summary(categories, alerts, suppressed, snapshot_summary, window)
    evidence = global_evidence(window, subscription, snapshot, files_info, chains,
                               categories, categories["duplicates"], errors)

    result_mirror = {
        "new_items": categories["new_items"],
        "updates": categories["updates"],
        "retractions": categories["retractions"],
        "already_seen": categories["already_seen"],
        "out_of_window": categories["out_of_window"],
        "late_indexed": categories["late_indexed"],
        "duplicates": categories["duplicates"],
        "alerts": alerts,
        "suppressed": suppressed,
        "window": {key: window.get(key) for key in
                   ("start_iso", "end_iso", "timezone", "complete", "locator", "end_inclusive")},
        "snapshot": snapshot_summary,
        "next_snapshot": next_snapshot,
        "replay": {
            "entry": "scripts/sk_entry.py",
            "command": "python scripts/sk_entry.py --input /input --work /app/work --out /app/output",
            "dedup_key_priority": POLICY.get("dedup_priority"),
            "inputs": [{"file": item.get("rel"), "sha256": item.get("sha256")}
                       for item in files_info],
            "previous_snapshot_locator": snapshot.get("locator"),
            "previous_watermark": snapshot.get("watermark"),
            "deterministic": True,
            "idempotency_note": "The previous snapshot is never mutated in place; feeding "
                                "next_snapshot back yields zero new items.",
        },
        "limitations": [
            "Only the supplied files are used; no live source is queried.",
            "Delayed indexing can only be recovered by re-running the same window.",
            "Retraction/correction detection relies on explicit markers in the source.",
            "Naive timestamps are echoed with the declared timezone, not silently shifted.",
            "PDF and unstructured text are not parsed into records.",
        ],
    }

    run_log = [
        {"step": "discover_input", "case_file": str(case_path) if case_path else None,
         "input_dir": str(input_dir), "files_listed": len(file_entries)},
        {"step": "parse_case", "case_id": str(case.get("case_id", "")) if case else "",
         "contract_keys": sorted(contract.keys()), "case_error": bool(case_error)},
        {"step": "parse_window", "start": window.get("start_iso"), "end": window.get("end_iso"),
         "timezone": window.get("timezone"), "complete": window.get("complete")},
        {"step": "read_snapshot", "locator": snapshot.get("locator"),
         "keys": len(snapshot.get("keys") or []), "records": len(snapshot.get("records") or []),
         "watermark": snapshot.get("watermark")},
        {"step": "collect_records", "files_scanned": len(files_info),
         "raw_units": len(units), "parse_errors": len(errors)},
        {"step": "deduplicate", "clusters": len(clusters),
         "duplicate_groups": len(categories["duplicates"])},
        {"step": "link_versions", "chains": len(chains),
         "multi_version_chains": sum(1 for chain in chains
                                     if len(chain.get("versions") or []) > 1)},
        {"step": "snapshot_diff", "new": len(categories["new_items"]),
         "updates": len(categories["updates"]), "retractions": len(categories["retractions"]),
         "already_seen": len(categories["already_seen"]),
         "out_of_window": len(categories["out_of_window"]),
         "late_indexed": len(categories["late_indexed"])},
        {"step": "alerts", "alerts": len(alerts), "suppressed": len(suppressed),
         "max_items": subscription.get("alert_rules", {}).get("max_items")},
        {"step": "snapshot_next", "keys": next_keys, "watermark": next_snapshot.get("watermark")},
        {"step": "contract", "declared": len(contract), "mapped": len(contract),
         "extras": 0},
        {"step": "write_output", "result": "result.json", "review": "run-review.md"},
    ]

    payload = {
        "case_id": str(case.get("case_id", "")) if isinstance(case, dict) else "",
        "status": status,
        "status_reason": status_reason,
        "summary": summary,
        "subscription": {
            "name": subscription.get("name", ""),
            "query": subscription.get("query", ""),
            "query_source": subscription.get("query_source", "subscription"),
            "sources": subscription.get("sources", []),
            "alert_rules": subscription.get("alert_rules", {}),
            "raw_keys": sorted(str(key) for key in subscription.keys()),
        },
        "window": {
            "start": window.get("start_iso"),
            "end": window.get("end_iso"),
            "timezone": window.get("timezone"),
            "complete": window.get("complete"),
            "end_inclusive": True,
            "source_locator": window.get("locator"),
            "raw_start": window.get("raw_start"),
            "raw_end": window.get("raw_end"),
        },
        "new_items": categories["new_items"],
        "updates": categories["updates"],
        "retractions": categories["retractions"],
        "already_seen": categories["already_seen"],
        "out_of_window": categories["out_of_window"],
        "late_indexed": categories["late_indexed"],
        "duplicates": categories["duplicates"],
        "alerts": alerts,
        "suppressed": suppressed,
        "evidence": evidence,
        "snapshot": snapshot_summary,
        "next_snapshot": next_snapshot,
        "uncertainty": uncertainties,
        "run_log": run_log,
        "result": result_mirror,
        "errors": errors,
        "stats": {
            "files_scanned": len(files_info),
            "files_parsed": sum(1 for item in files_info if item.get("status") == "parsed"),
            "records_raw": len(units),
            "clusters": len(clusters),
            "chains": len(chains),
            "version_chains": sum(1 for chain in chains
                                  if len(chain.get("versions") or []) > 1),
            "new_items": len(categories["new_items"]),
            "updates": len(categories["updates"]),
            "retractions": len(categories["retractions"]),
            "already_seen": len(categories["already_seen"]),
            "out_of_window": len(categories["out_of_window"]),
            "late_indexed": len(categories["late_indexed"]),
            "duplicates": len(categories["duplicates"]),
            "alerts": len(alerts),
            "suppressed": len(suppressed),
            "evidence": len(evidence),
            "previous_snapshot_keys": previous_keys,
            "next_snapshot_keys": next_keys,
            "network": "disabled",
            "deterministic": True,
        },
        "contract_extras": {},
        "contract": {},
    }
    payload = apply_contract(payload, case)
    return payload


def _line_is_json(line):
    try:
        json.loads(line)
        return True
    except ValueError:
        return False


def _looks_tabular(text):
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line
            break
    if not first:
        return False
    low = first.lower()
    return any(token in low for token in ("title", "doi", "pmid", "authors")) and \
        any(delim in first for delim in ("\t", ",", ";", "|"))


# ---------------------------------------------------------------------------
# self-check and review
# ---------------------------------------------------------------------------

def self_check(payload, contract):
    issues = []
    required = ["case_id", "status", "subscription", "window", "new_items", "updates",
                "retractions", "already_seen", "out_of_window", "duplicates", "alerts",
                "suppressed", "evidence", "snapshot", "uncertainty", "run_log"]
    for key in required:
        if key not in payload:
            issues.append("missing required field: " + key)
    for key in contract:
        if key not in payload:
            issues.append("missing output_contract field: " + key)
    for category in ("new_items", "updates", "retractions", "already_seen",
                     "out_of_window", "late_indexed"):
        for item in payload.get(category, []):
            if not item.get("evidence"):
                issues.append("%s item without evidence: %s"
                              % (category, item.get("dedup_key")))
    stats = payload.get("stats", {})
    for key in ("new_items", "updates", "retractions", "already_seen",
                "out_of_window", "late_indexed", "duplicates", "alerts"):
        if stats.get(key) != len(payload.get(key, [])):
            issues.append("stats mismatch for " + key)
    if stats.get("previous_snapshot_keys", 0) > stats.get("next_snapshot_keys", 0):
        issues.append("snapshot key count decreased")
    return issues


def write_json(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, str(target))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_text(path, text):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


REVIEW_MARKER = "<!-- generated-by: sk_entry.py -->"


def review_text(payload):
    lines = [REVIEW_MARKER, "", "# 运行复核（脚本自动统计，尚未逐条人工核验）", ""]
    lines.append("- case_id: `%s`" % payload.get("case_id", ""))
    lines.append("- status: `%s` — %s" % (payload.get("status"), payload.get("status_reason", "")))
    window = payload.get("window", {})
    lines.append("- window: %s .. %s (timezone=%s, complete=%s)"
                 % (window.get("start"), window.get("end"),
                    window.get("timezone"), window.get("complete")))
    stats = payload.get("stats", {})
    lines.append("- files scanned/parsed: %s / %s"
                 % (stats.get("files_scanned"), stats.get("files_parsed")))
    lines.append("- records raw → clusters → chains: %s → %s → %s"
                 % (stats.get("records_raw"), stats.get("clusters"), stats.get("chains")))
    lines.append("- new / updates / retractions / already_seen / out_of_window / late_indexed: "
                 "%s / %s / %s / %s / %s / %s"
                 % (stats.get("new_items"), stats.get("updates"), stats.get("retractions"),
                    stats.get("already_seen"), stats.get("out_of_window"),
                    stats.get("late_indexed")))
    lines.append("- duplicate groups / alerts / suppressed: %s / %s / %s"
                 % (stats.get("duplicates"), stats.get("alerts"), stats.get("suppressed")))
    snapshot = payload.get("snapshot", {})
    lines.append("- snapshot keys: %s → %s; watermark=%s; advanced=%s"
                 % (snapshot.get("previous_keys"), snapshot.get("next_keys"),
                    snapshot.get("watermark"), snapshot.get("advanced")))
    lines.append("")
    lines.append("## 证据抽样（逐条复核请打开 result.json 的 evidence / source_locators）")
    shown = 0
    for category in ("new_items", "updates", "retractions", "already_seen",
                     "out_of_window", "late_indexed"):
        for item in payload.get(category, [])[:10]:
            locs = item.get("evidence") or []
            loc = ""
            if locs:
                first = locs[0]
                loc = "%s" % first.get("file", "")
                for key in ("pointer", "line", "row", "xpath"):
                    if first.get(key) not in (None, ""):
                        loc += " %s=%s" % (key, first[key])
            lines.append("- [%s] %s — %s" % (category, item.get("title", ""), loc))
            shown += 1
    if shown == 0:
        lines.append("- 本次没有可列出的条目（空窗口也是有效结果）。")
    lines.append("")
    lines.append("## 不确定项")
    for item in payload.get("uncertainty", [])[:30]:
        lines.append("- `%s`: %s" % (item.get("type"), item.get("detail", "")))
    lines.append("")
    lines.append("## 执行 Agent 必须做的事")
    lines.append("1. 重新运行：`python /skills/weekly-skill/scripts/sk_entry.py --input /input --work /app/work --out /app/output`。")
    lines.append("2. 打开 `/app/output/result.json`，逐条核对 new_items/updates/retractions 的计数与 evidence 中的 file + pointer/line。")
    lines.append("3. 把核对结论、实际找到的内容与仍不确定的部分写入 `/app/output/run-review.md`；不得补造不存在的文献、日期或结论。")
    lines.append("4. 若发现来源文件未被解析，先在 run-review.md 说明，再按 SKILL.md 的异常处理章节处置。")
    return "\n".join(lines) + "\n"


def write_review(out_dir, payload):
    path = Path(out_dir) / "run-review.md"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8", errors="replace")
            if not existing.startswith(REVIEW_MARKER):
                return  # keep the executing agent's verified review
        write_text(path, review_text(payload))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def resolve_out(out_arg):
    text = str(out_arg or DEFAULT_OUT)
    if text.lower().endswith(".json"):
        return Path(text), Path(text).parent
    return Path(text) / "result.json", Path(text)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Incremental literature discovery and tracking (stdlib only)")
    parser.add_argument("--input", "-i", default=DEFAULT_INPUT,
                        help="input directory or case JSON (default: /input)")
    parser.add_argument("--work", "-w", default=DEFAULT_WORK,
                        help="working directory (default: /app/work)")
    parser.add_argument("--out", "-o", default=DEFAULT_OUT,
                        help="output directory or result JSON path (default: /app/output)")
    parser.add_argument("--case", default=None,
                        help="backward-compatible alias for --input")
    parser.add_argument("--output", default=None,
                        help="backward-compatible alias for --out")
    args = parser.parse_args(argv)

    input_arg = args.case or args.input
    out_arg = args.output or args.out
    result_path, out_dir = resolve_out(out_arg)
    work_dir = Path(args.work)
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    case_path, input_dir = resolve_input(input_arg)
    case = {}
    case_error = None
    case_loaded_path = None
    try:
        case, case_loaded_path, case_error = discover_case(case_path, input_dir)
    except Exception as exc:  # noqa: BLE001
        case_error = {"stage": "discover_case", "type": type(exc).__name__,
                      "message": str(exc)[:300]}
    task_text = ""
    for candidate in (Path(input_dir) / "task.md", Path(input_dir) / "task.txt"):
        try:
            if candidate.is_file():
                task_text = candidate.read_text(encoding="utf-8", errors="replace")
                break
        except OSError:
            continue

    try:
        payload = build_payload(case, case_loaded_path or case_path, input_dir,
                                case_error, task_text, [out_dir, work_dir])
        contract = extract_contract(case)
        issues = self_check(payload, contract)
        if issues:
            payload.setdefault("uncertainty", []).append({
                "type": "self_check",
                "detail": "; ".join(issues[:20]),
                "resolution": "status was downgraded to partial; see errors/uncertainty",
            })
            if payload.get("status") == "success":
                payload["status"] = "partial"
                payload["status_reason"] = (payload.get("status_reason", "")
                                            + " Self-check reported issues.")
            payload["stats"]["self_check_issues"] = len(issues)
        else:
            payload["stats"]["self_check_issues"] = 0
    except Exception as exc:  # noqa: BLE001 - always write a valid result
        case_id = ""
        if isinstance(case, dict):
            case_id = str(case.get("case_id", "") or "")
        payload = {
            "case_id": case_id,
            "status": "partial",
            "status_reason": "Unexpected build error; no incremental conclusion is claimed.",
            "subscription": {"name": "", "query": "", "sources": [],
                             "alert_rules": POLICY.get("alert_caps", {}), "raw_keys": []},
            "window": {"start": None, "end": None, "timezone": None, "complete": False,
                       "end_inclusive": True, "source_locator": None},
            "new_items": [], "updates": [], "retractions": [], "already_seen": [],
            "out_of_window": [], "late_indexed": [], "duplicates": [], "alerts": [],
            "suppressed": [], "evidence": [],
            "snapshot": {"previous_keys": 0, "current_keys": 0, "next_keys": 0,
                         "watermark": None, "locator": "", "advanced": False},
            "next_snapshot": {"watermark": None, "keys": [], "records": []},
            "uncertainty": [{"type": "build_error", "detail": str(exc)[:300],
                             "resolution": "no records were fabricated"}],
            "run_log": [{"step": "build", "status": "error",
                         "type": type(exc).__name__}],
            "result": None,
            "errors": [{"stage": "build", "type": type(exc).__name__,
                        "message": str(exc)[:500]}],
            "stats": {"network": "disabled", "deterministic": True},
            "contract_extras": {}, "contract": {},
            "summary": "构建过程异常，未产出增量结论。",
        }
        try:
            payload = apply_contract(payload, case if isinstance(case, dict) else {})
        except Exception:  # noqa: BLE001
            pass

    try:
        write_json(result_path, payload)
        write_review(out_dir, payload)
        try:
            write_json(work_dir / "run_manifest.json", {
                "case_id": payload.get("case_id"),
                "status": payload.get("status"),
                "window": payload.get("window"),
                "stats": payload.get("stats"),
                "input_files": payload.get("replay", {}).get("inputs", []),
            })
            write_json(work_dir / "next_snapshot.json", payload.get("next_snapshot") or {})
        except OSError:
            pass
    except OSError as exc:
        sys.stderr.write(json.dumps({"status": "failed", "stage": "write_output",
                                     "message": str(exc)[:300]}, ensure_ascii=False) + "\n")
        return 3

    sys.stdout.write(json.dumps({
        "status": payload.get("status"),
        "case_id": payload.get("case_id"),
        "result": str(result_path),
        "new_items": len(payload.get("new_items") or []),
        "updates": len(payload.get("updates") or []),
        "retractions": len(payload.get("retractions") or []),
        "alerts": len(payload.get("alerts") or []),
    }, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
