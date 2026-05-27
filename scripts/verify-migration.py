#!/usr/bin/env python3
"""迁后校验：比对源端与目标端文件完整性，输出校验报告"""

import os
import csv
import hashlib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = PROJECT_ROOT / "02-downloads"
STAGING_DIR = PROJECT_ROOT / "03-staging"
SCAN_REPORT = PROJECT_ROOT / "01-source-audit" / "scan-report.csv"
INTEGRITY_REPORT = PROJECT_ROOT / "05-verification" / "integrity-report.csv"


def md5_hash(filepath: Path) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_scan_report() -> dict:
    if not SCAN_REPORT.exists():
        return {}
    source_files = {}
    with open(SCAN_REPORT, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            source_files[row["文件名"]] = {
                "md5": row["MD5"],
                "size": int(row["大小_字节"]),
                "source_path": row["相对路径"],
            }
    return source_files


def scan_staging() -> dict:
    staging_files = {}
    for root, _, filenames in os.walk(STAGING_DIR):
        for fname in filenames:
            if fname.startswith("."):
                continue
            fpath = Path(root) / fname
            stat = fpath.stat()
            staging_files[fname] = {
                "md5": md5_hash(fpath),
                "size": stat.st_size,
                "staging_path": str(fpath.relative_to(STAGING_DIR)),
            }
    return staging_files


def verify():
    print("加载源端扫描报告...")
    source = load_scan_report()
    if not source:
        print("警告：未找到scan-report.csv，请先运行 scan-source.py")
        print("将仅统计staging目录...")

    print("扫描staging目录...")
    staging = scan_staging()

    results = []
    stats = {"matched": 0, "hash_mismatch": 0, "missing_in_staging": 0, "extra_in_staging": 0}

    if source:
        for fname, src_info in source.items():
            if fname in staging:
                stg_info = staging[fname]
                if src_info["md5"] == stg_info["md5"]:
                    status = "一致"
                    stats["matched"] += 1
                else:
                    status = "哈希不一致"
                    stats["hash_mismatch"] += 1
                results.append({
                    "文件名": fname,
                    "源路径": src_info["source_path"],
                    "目标路径": stg_info["staging_path"],
                    "源MD5": src_info["md5"],
                    "目标MD5": stg_info["md5"],
                    "源大小": src_info["size"],
                    "目标大小": stg_info["size"],
                    "校验结果": status,
                })
            else:
                stats["missing_in_staging"] += 1
                results.append({
                    "文件名": fname,
                    "源路径": src_info["source_path"],
                    "目标路径": "",
                    "源MD5": src_info["md5"],
                    "目标MD5": "",
                    "源大小": src_info["size"],
                    "目标大小": "",
                    "校验结果": "staging缺失",
                })

        for fname, stg_info in staging.items():
            if fname not in source:
                stats["extra_in_staging"] += 1
                results.append({
                    "文件名": fname,
                    "源路径": "",
                    "目标路径": stg_info["staging_path"],
                    "源MD5": "",
                    "目标MD5": stg_info["md5"],
                    "源大小": "",
                    "目标大小": stg_info["size"],
                    "校验结果": "源端无记录（新增文件）",
                })
    else:
        for fname, stg_info in staging.items():
            results.append({
                "文件名": fname,
                "源路径": "",
                "目标路径": stg_info["staging_path"],
                "源MD5": "",
                "目标MD5": stg_info["md5"],
                "源大小": "",
                "目标大小": stg_info["size"],
                "校验结果": "仅统计（无源端基准）",
            })

    INTEGRITY_REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(INTEGRITY_REPORT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "文件名", "源路径", "目标路径", "源MD5", "目标MD5", "源大小", "目标大小", "校验结果"
        ])
        writer.writeheader()
        writer.writerows(results)

    print(f"\n校验完成：")
    print(f"  源端文件总数：{len(source)}")
    print(f"  staging文件总数：{len(staging)}")
    if source:
        print(f"  哈希一致：{stats['matched']}")
        print(f"  哈希不一致：{stats['hash_mismatch']}")
        print(f"  staging缺失：{stats['missing_in_staging']}")
        print(f"  staging多余：{stats['extra_in_staging']}")
    print(f"  报告：{INTEGRITY_REPORT}")

    if stats["hash_mismatch"] > 0 or stats["missing_in_staging"] > 0:
        print("\n存在异常，请检查报告后再执行上传！")
        return False
    return True


if __name__ == "__main__":
    verify()
