#!/usr/bin/env python3
"""扫描02-downloads目录，生成全量文件清单到01-source-audit/scan-report.csv"""

import os
import csv
import hashlib
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "02-downloads"
REPORT_PATH = PROJECT_ROOT / "01-source-audit" / "scan-report.csv"
FOLDER_SUMMARY_PATH = PROJECT_ROOT / "01-source-audit" / "folder-summary.csv"


def md5_hash(filepath: Path) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_downloads():
    if not DOWNLOADS_DIR.exists():
        print(f"错误：下载目录不存在 {DOWNLOADS_DIR}")
        return

    files = []
    for root, _, filenames in os.walk(DOWNLOADS_DIR):
        for fname in filenames:
            fpath = Path(root) / fname
            if fpath.name.startswith("."):
                continue
            stat = fpath.stat()
            files.append({
                "文件名": fpath.name,
                "相对路径": str(fpath.relative_to(DOWNLOADS_DIR)),
                "扩展名": fpath.suffix.lower(),
                "大小_字节": stat.st_size,
                "大小_可读": format_size(stat.st_size),
                "修改时间": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "MD5": md5_hash(fpath),
                "批次": fpath.relative_to(DOWNLOADS_DIR).parts[0] if len(fpath.relative_to(DOWNLOADS_DIR).parts) > 1 else "root",
            })

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(REPORT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["文件名", "相对路径", "扩展名", "大小_字节", "大小_可读", "修改时间", "MD5", "批次"])
        writer.writeheader()
        writer.writerows(files)

    print(f"扫描完成：共 {len(files)} 个文件")
    print(f"报告已保存至：{REPORT_PATH}")

    write_folder_summary(files)

    ext_stats = {}
    for f in files:
        ext = f["扩展名"] or "(无扩展名)"
        ext_stats[ext] = ext_stats.get(ext, 0) + 1
    print("\n文件类型统计：")
    for ext, count in sorted(ext_stats.items(), key=lambda x: -x[1]):
        print(f"  {ext}: {count}")


def write_folder_summary(files):
    """产出文件夹层级汇总：每层目录的直接/递归文件数与大小。"""
    # direct[dir] = 直接文件；recursive[dir] = 含所有子孙文件（累加到每个祖先目录）
    direct_count, direct_size = {}, {}
    recur_count, recur_size = {}, {}

    def touch(store_c, store_s, key, size):
        store_c[key] = store_c.get(key, 0) + 1
        store_s[key] = store_s.get(key, 0) + size

    for f in files:
        rel = Path(f["相对路径"])
        size = f["大小_字节"]
        parent = rel.parent  # 相对 DOWNLOADS_DIR，root 表示为 "."
        touch(direct_count, direct_size, parent, size)
        # 累加到该文件的每一层祖先目录（含 root "."）
        ancestors = list(rel.parents)  # 已含 Path(".")
        for anc in ancestors:
            touch(recur_count, recur_size, anc, size)

    all_dirs = set(direct_count) | set(recur_count)

    def dir_label(p):
        return "(根目录)" if str(p) == "." else str(p)

    def depth(p):
        return 0 if str(p) == "." else len(p.parts)

    rows = []
    for d in sorted(all_dirs, key=lambda p: (depth(p), str(p))):
        rows.append({
            "文件夹": dir_label(d),
            "层级": depth(d),
            "直接文件数": direct_count.get(d, 0),
            "递归文件数": recur_count.get(d, 0),
            "直接大小_字节": direct_size.get(d, 0),
            "直接大小_可读": format_size(direct_size.get(d, 0)),
            "递归大小_字节": recur_size.get(d, 0),
            "递归大小_可读": format_size(recur_size.get(d, 0)),
        })

    fieldnames = ["文件夹", "层级", "直接文件数", "递归文件数",
                  "直接大小_字节", "直接大小_可读", "递归大小_字节", "递归大小_可读"]
    with open(FOLDER_SUMMARY_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"文件夹层级汇总已保存至：{FOLDER_SUMMARY_PATH}（共 {len(rows)} 层目录）")


def format_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


if __name__ == "__main__":
    scan_downloads()
