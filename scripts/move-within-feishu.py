#!/usr/bin/env python3
"""飞书云盘内 movement —— 把上传到 staging 文件夹的 WPS 源文件夹，
按 wps-folder-mapping.json 移动到飞书的目标文件夹，并按文件名查重跳过。

使用场景：
  你先把 WPS 云盘里的源文件夹（保留原有目录结构）上传到飞书的 staging 文件夹
  （默认 BtDzfm0djl1anldpo8ac0ulrnSg），然后跑本脚本，把里面的文件逐个移动到
  各自的飞书目标文件夹里。

流程：
  1. 递归遍历 staging 文件夹，得到每个文件相对 staging 的路径（= WPS 源相对路径）。
  2. 复用 upload-to-feishu.py 的 classify_file，按映射定位目标 folder_token + 子路径。
  3. ensure_folder_path 确保目标子路径存在（tokens 已齐，通常直接命中）。
  4. 文件名查重：目标夹里已有同名文件 → 跳过；否则用飞书 move API 移动。

移动是「原地移动」，不复制、不重复上传，天然幂等：已移走的文件不在 staging 里，
重跑不会重复；目标夹已有同名文件也会跳过。
"""

import os
import sys
import csv
import time
import argparse
import importlib.util
from datetime import datetime
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
UPLOAD_LOG_DIR = PROJECT_ROOT / "04-upload-logs"

DEFAULT_STAGING_TOKEN = "BtDzfm0djl1anldpo8ac0ulrnSg"


def _load_upload_module():
    """以 importlib 加载 upload-to-feishu.py（文件名含连字符，无法直接 import），
    复用其 classify_file / 配置加载 / 飞书 API 辅助函数，保证映射逻辑单一来源。"""
    path = SCRIPTS_DIR / "upload-to-feishu.py"
    spec = importlib.util.spec_from_file_location("upload_to_feishu", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


U = _load_upload_module()
FEISHU_BASE = U.FEISHU_BASE


def collect_staging_files(token: str, staging_token: str,
                          rel: tuple[str, ...] = ()) -> list[dict]:
    """递归遍历 staging 文件夹，返回所有文件（非文件夹）的清单。
    每项：{rel_parts, file_token, name, type}，rel_parts 含文件名，相对 staging 根。"""
    files: list[dict] = []
    for item in U.list_folder_cached(token, staging_token):
        name = item.get("name", "")
        itype = item.get("type", "")
        itoken = item.get("token", "")
        if itype == "folder":
            files.extend(collect_staging_files(token, itoken, rel + (name,)))
        else:
            files.append({
                "rel_parts": rel + (name,),
                "file_token": itoken,
                "name": name,
                "type": itype,
            })
    return files


def move_file(token: str, file_token: str, dest_folder_token: str) -> dict:
    """飞书云盘内移动文件到目标文件夹。"""
    url = f"{FEISHU_BASE}/drive/v1/files/{file_token}/move"
    resp = requests.post(url, headers=U.get_headers(token), json={
        "type": "file",
        "folder_token": dest_folder_token,
    })
    data = resp.json()
    if data.get("code") == 0:
        return {"status": "success", "task_id": data.get("data", {}).get("task_id", "")}
    return {"status": "failed", "error": data.get("msg", resp.text)}


def run_move(dry_run: bool = False, staging_token: str = DEFAULT_STAGING_TOKEN,
             source_prefix: str | None = None, limit: int | None = None):
    config = U.load_config()
    settings = config.get("upload_settings", {})

    token = ""
    if not dry_run:
        token = U.get_tenant_access_token()
        if not token:
            return
    else:
        # dry-run 也需要 token 才能读取 staging 结构（只读 GET）
        token = U.get_tenant_access_token()
        if not token:
            print("提示：dry-run 需要飞书凭证才能读取 staging 结构")
            return

    mode_label = "dry-run 模拟" if dry_run else "移动"
    print(f"模式：{mode_label}")
    print(f"staging 文件夹：{staging_token}")
    if source_prefix:
        print(f"过滤前缀：{source_prefix}")
    if limit:
        print(f"文件数上限：{limit}")
    print()

    print("遍历 staging 文件夹…")
    staged = collect_staging_files(token, staging_token)
    print(f"staging 内共 {len(staged)} 个文件\n")

    # 每个目标 folder_token 维护一份「已存在文件名」集合，用于文件名查重。
    # 初始按目标夹现有文件填充，移动成功后追加，保证同一次运行内也不重复。
    target_name_cache: dict[str, set[str]] = {}

    def existing_names(dest_token: str) -> set[str]:
        if dest_token not in target_name_cache:
            names = {it.get("name", "") for it in U.list_folder_cached(token, dest_token)
                     if it.get("type") != "folder"}
            target_name_cache[dest_token] = names
        return target_name_cache[dest_token]

    log_entries = []
    stats: dict[str, int] = {}
    processed = 0

    for f in staged:
        rel_parts = f["rel_parts"]
        rel_str = "/".join(rel_parts)
        name = f["name"]

        if source_prefix and not rel_str.lower().startswith(source_prefix.lower()):
            continue
        if limit and processed >= limit:
            break

        label, folder_token, sub_path = U.classify_file(rel_parts, config)
        if not folder_token:
            print(f"  跳过（无目标文件夹）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "跳过-无目标",
                "目标folder_token": "", "时间": datetime.now().isoformat(), "错误": "",
            })
            stats["skip_no_target"] = stats.get("skip_no_target", 0) + 1
            continue

        processed += 1

        # 目标子路径（去掉文件名那一层）
        rel_dir = sub_path.parent if sub_path != Path(".") else Path(".")

        if dry_run:
            desc = folder_token + (f" / {rel_dir}" if rel_dir != Path(".") else "")
            # dry-run 只对已存在目标夹做查重预判（不创建子文件夹）
            dedup_hint = ""
            print(f"  [{label}] {rel_str}")
            print(f"    → 目标: {desc}{dedup_hint}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "dry-run",
                "目标folder_token": folder_token, "子路径": str(sub_path),
                "时间": datetime.now().isoformat(), "错误": "",
            })
            stats[label] = stats.get(label, 0) + 1
            continue

        # 确保目标子路径存在
        if rel_dir != Path("."):
            dest_token = U.ensure_folder_path(token, folder_token, rel_dir)
        else:
            dest_token = folder_token
        if not dest_token:
            print(f"  失败（无法定位/创建目标文件夹）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "失败",
                "目标folder_token": folder_token, "子路径": str(sub_path),
                "时间": datetime.now().isoformat(), "错误": "无法创建目标文件夹",
            })
            stats["failed"] = stats.get("failed", 0) + 1
            continue

        # 文件名查重：目标夹已有同名文件 → 跳过
        if name in existing_names(dest_token):
            print(f"  跳过（同名已存在）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "跳过-同名",
                "目标folder_token": dest_token, "子路径": str(sub_path),
                "时间": datetime.now().isoformat(), "错误": "",
            })
            stats["skip_dup"] = stats.get("skip_dup", 0) + 1
            continue

        # 移动（带重试）
        result = None
        for _ in range(settings.get("retry_max", 3)):
            result = move_file(token, f["file_token"], dest_token)
            if result["status"] == "success":
                break
            time.sleep(settings.get("retry_delay_seconds", 5))

        if result and result["status"] == "success":
            existing_names(dest_token).add(name)  # 本次已移入，后续同名跳过
            print(f"  [{label}] 移动: {rel_str}")
            stats[label] = stats.get(label, 0) + 1
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "success",
                "目标folder_token": dest_token, "子路径": str(sub_path),
                "时间": datetime.now().isoformat(), "错误": "",
            })
        else:
            err = result.get("error", "unknown") if result else "unknown"
            print(f"  失败: {rel_str} — {err}")
            stats["failed"] = stats.get("failed", 0) + 1
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "失败",
                "目标folder_token": dest_token, "子路径": str(sub_path),
                "时间": datetime.now().isoformat(), "错误": err,
            })

    # 写日志
    UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    suffix = "-dryrun" if dry_run else ""
    log_path = UPLOAD_LOG_DIR / f"move-report-{date_str}{suffix}.csv"
    fieldnames = ["源路径", "路由", "状态", "目标folder_token", "子路径", "时间", "错误"]
    with open(log_path, "w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(log_entries)

    failed = stats.pop("failed", 0)
    skip_dup = stats.pop("skip_dup", 0)
    skip_no_target = stats.pop("skip_no_target", 0)
    total_ok = sum(stats.values())
    detail = " / ".join(f"{k} {v}" for k, v in sorted(stats.items()) if v > 0)

    print(f"\n{mode_label}完成：")
    print(f"  处理：{total_ok}（{detail}）")
    print(f"  同名跳过：{skip_dup}")
    print(f"  无目标跳过：{skip_no_target}")
    if not dry_run:
        print(f"  失败：{failed}")
    print(f"  日志：{log_path}")


def main():
    parser = argparse.ArgumentParser(description="飞书云盘内 movement 工具")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行，只显示路由与查重结果，不实际移动")
    parser.add_argument("--staging", type=str, default=DEFAULT_STAGING_TOKEN,
                        help=f"staging 文件夹 token（默认 {DEFAULT_STAGING_TOKEN}）")
    parser.add_argument("--source-prefix", type=str, default=None,
                        help="只处理指定前缀的源路径")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多处理 N 个文件")
    args = parser.parse_args()
    run_move(dry_run=args.dry_run, staging_token=args.staging,
             source_prefix=args.source_prefix, limit=args.limit)


if __name__ == "__main__":
    main()
