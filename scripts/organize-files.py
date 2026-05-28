#!/usr/bin/env python3
"""按archive-rules.yaml将02-downloads中的文件分类整理到03-staging目录"""

import os
import re
import json
import shutil
import csv
import yaml
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "00-config" / "archive-rules.yaml"
SOURCE_CONFIG_PATH = PROJECT_ROOT / "00-config" / "source-account.json"
DOWNLOADS_DIR = PROJECT_ROOT / "02-downloads"
STAGING_DIR = PROJECT_ROOT / "03-staging"
UNCLASSIFIED_DIR = STAGING_DIR / "待分类"
LOG_PATH = PROJECT_ROOT / "06-docs" / "organize-log.csv"


def load_rules() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_source_mappings() -> list:
    if not SOURCE_CONFIG_PATH.exists():
        return []
    with open(SOURCE_CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("source_paths", [])


def extract_year_month(filename: str, filepath: Path) -> tuple:
    patterns = [
        r"(\d{4})[-_年]?(\d{1,2})[-_月]?",
        r"(\d{4})(\d{2})",
    ]
    for p in patterns:
        m = re.search(p, filename)
        if m:
            year = int(m.group(1))
            month = int(m.group(2))
            if 2000 <= year <= 2099 and 1 <= month <= 12:
                return year, month

    m = re.search(r"(\d{4})", filename)
    if m:
        year = int(m.group(1))
        if 2000 <= year <= 2099:
            return year, None

    mtime = datetime.fromtimestamp(filepath.stat().st_mtime)
    return mtime.year, mtime.month


def detect_entity(filepath: Path, mappings: list) -> str | None:
    rel_path_str = str(filepath.relative_to(DOWNLOADS_DIR))
    for mapping in mappings:
        wps_path = mapping.get("wps_path", "").strip("/")
        if wps_path and wps_path.lower() in rel_path_str.lower():
            return mapping.get("entity")
    return None


def match_by_source_mapping(filepath: Path, mappings: list, rules: dict) -> dict | None:
    rel_path_str = str(filepath.relative_to(DOWNLOADS_DIR))
    for mapping in mappings:
        wps_path = mapping.get("wps_path", "").strip("/")
        if wps_path and wps_path.lower() in rel_path_str.lower():
            target_cat = mapping.get("target_category", "")
            for period_name, period_config in rules["retention_periods"].items():
                for cat in period_config["categories"]:
                    if cat["name"] == target_cat:
                        return {
                            "type": "会计档案",
                            "period": period_name,
                            "category": cat["name"],
                            "subfolder_by": cat.get("subfolder_by"),
                            "subtypes": cat.get("subtypes"),
                        }
            for cat in rules.get("non_accounting", {}).get("categories", []):
                if cat["name"] == target_cat:
                    return {
                        "type": "非会计档案",
                        "period": None,
                        "category": cat["name"],
                        "subfolder_by": None,
                        "subtypes": None,
                    }
    return None


def match_category(filename: str, filepath: Path, rules: dict, mappings: list = None) -> dict | None:
    if mappings:
        result = match_by_source_mapping(filepath, mappings, rules)
        if result:
            return result

    fname_lower = filename.lower()
    rel_path_str = str(filepath.relative_to(DOWNLOADS_DIR)).lower()
    search_text = rel_path_str + " " + fname_lower

    for period_name, period_config in rules["retention_periods"].items():
        for cat in period_config["categories"]:
            for kw in cat.get("keywords", []):
                if kw.lower() in search_text:
                    return {
                        "type": "会计档案",
                        "period": period_name,
                        "category": cat["name"],
                        "subfolder_by": cat.get("subfolder_by"),
                        "subtypes": cat.get("subtypes"),
                    }

    for cat in rules.get("non_accounting", {}).get("categories", []):
        for kw in cat.get("keywords", []):
            if kw.lower() in search_text:
                return {
                    "type": "非会计档案",
                    "period": None,
                    "category": cat["name"],
                    "subfolder_by": None,
                    "subtypes": None,
                }

    return None


def determine_subtype(filename: str, subtypes: list) -> str | None:
    fname_lower = filename.lower()
    for st in subtypes:
        if st.lower() in fname_lower:
            return st
    return subtypes[0] if subtypes else None


def build_target_path(match: dict, year: int, month: int | None, entity: str | None = None) -> Path:
    if match["type"] == "会计档案":
        if entity:
            base = STAGING_DIR / "会计档案" / entity / match["category"]
        else:
            base = STAGING_DIR / "会计档案" / match["category"]

        if match.get("subtypes"):
            subtype = match.get("_subtype", match["subtypes"][0])
            base = base / subtype

        if match.get("subfolder_by") == "month" and month:
            return base / str(year) / f"{month:02d}"
        elif year:
            return base / str(year)
        return base
    else:
        base = STAGING_DIR / "非会计档案" / match["category"]
        if year:
            return base / str(year)
        return base


def organize():
    if not DOWNLOADS_DIR.exists():
        print(f"错误：下载目录不存在 {DOWNLOADS_DIR}")
        return

    rules = load_rules()
    mappings = load_source_mappings()
    log_entries = []
    stats = {"classified": 0, "unclassified": 0, "format_warning": 0}

    accounting_formats = rules.get("allowed_formats", {}).get("accounting", {})
    rejected_exts = set(accounting_formats.get("rejected", []))

    for root, _, filenames in os.walk(DOWNLOADS_DIR):
        for fname in filenames:
            if fname.startswith("."):
                continue

            src = Path(root) / fname
            ext = src.suffix.lower()
            year, month = extract_year_month(fname, src)

            match = match_category(fname, src, rules, mappings)

            if match is None:
                target_dir = UNCLASSIFIED_DIR
                target = target_dir / fname
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                stats["unclassified"] += 1
                log_entries.append({
                    "源文件": str(src.relative_to(DOWNLOADS_DIR)),
                    "目标路径": str(target.relative_to(STAGING_DIR)),
                    "分类": "待分类",
                    "保管期限": "",
                    "格式警告": "",
                })
                continue

            if match.get("subtypes"):
                match["_subtype"] = determine_subtype(fname, match["subtypes"])

            entity = detect_entity(src, mappings) if mappings else None
            target_dir = build_target_path(match, year, month, entity)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / fname
            shutil.copy2(src, target)
            stats["classified"] += 1

            format_warn = ""
            if match["type"] == "会计档案" and ext in rejected_exts:
                format_warn = f"不推荐格式{ext}，建议转换为PDF/OFD"
                stats["format_warning"] += 1

            log_entries.append({
                "源文件": str(src.relative_to(DOWNLOADS_DIR)),
                "目标路径": str(target.relative_to(STAGING_DIR)),
                "所属实体": entity or "",
                "分类": f"{match['type']}/{match.get('period', '')}/{match['category']}",
                "保管期限": match.get("period", ""),
                "格式警告": format_warn,
            })

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["源文件", "目标路径", "所属实体", "分类", "保管期限", "格式警告"])
        writer.writeheader()
        writer.writerows(log_entries)

    print(f"整理完成：")
    print(f"  已分类：{stats['classified']} 个文件")
    print(f"  待人工分类：{stats['unclassified']} 个文件")
    print(f"  格式警告：{stats['format_warning']} 个文件")
    print(f"  整理日志：{LOG_PATH}")

    if stats["unclassified"] > 0:
        print(f"\n请检查 {UNCLASSIFIED_DIR} 中的文件，手动归入正确目录")


if __name__ == "__main__":
    organize()
