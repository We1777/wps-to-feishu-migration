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
    每项：{rel_parts, file_token, src_folder_token, name, type}，rel_parts 含文件名，
    相对 staging 根；src_folder_token 为该文件当前所在（源）文件夹的 token。"""
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
                "src_folder_token": staging_token,  # 文件当前所在的源文件夹 token
                "name": name,
                "type": itype,
            })
    return files


def resolve_source_subtree(token: str, staging_token: str,
                           source_prefix: str) -> tuple[str, tuple[str, ...]]:
    """把 source_prefix 尽量解析成 staging 下对应的子文件夹 token，实现「直达子夹」：
    从 staging 根开始，逐段（按 '/' 切分，不区分大小写）在当前夹里找同名子文件夹并下钻，
    返回 (起始遍历 token, 已匹配的 rel 前缀)。

    只沿「完整匹配到的文件夹段」下钻——遇到某段不是现成子夹（是文件 / 部分名 / 不存在）
    就停在当前层。这样遍历起点尽量贴近目标夹，其余分支根本不枚举；而主循环的 startswith
    过滤仍对全 staging 相对路径生效，结果与全量扫描完全一致（只是不再陪跑整棵树）。"""
    cur_token = staging_token
    matched: list[str] = []
    for seg in [s for s in source_prefix.split("/") if s]:
        child = None
        for it in U.list_folder_cached(token, cur_token):
            if it.get("type") == "folder" and it.get("name", "").lower() == seg.lower():
                child = it.get("token", "")
                break
        if not child:
            break  # 该段无对应子夹 → 停在当前层，剩余交给 startswith 过滤
        cur_token = child
        matched.append(seg)
    return cur_token, tuple(matched)


def build_token_path_map(config: dict) -> dict[str, str]:
    """构建「飞书目标 folder_token → 人可读完整落地路径」反查表。
    路径含空间根（归档库/工作台/知识库/待定事项）+ 实体 + 中间层子目录，
    与 base 表「飞书目标文件夹」列同口径，用于对账 CSV 的「目标末级子文件夹」列，
    避免用「路由标签+源子路径」拼接时丢掉中间层（如 c. 会计凭证/FY-2025/i. 记账凭证）。"""
    m: dict[str, str] = {}

    # 1) 空间根
    for name, data in config.get("root_folders", {}).items():
        tk = data.get("folder_token", "")
        if tk:
            m[tk] = name

    # 2) 归档库下大陆实体 C1-C9
    for entity, tokens in config.get("entity_subfolder_tokens", {}).items():
        for key, tk in tokens.items():
            if not tk:
                continue
            m[tk] = f"归档库/{entity}" if key == "root" else f"归档库/{entity}/{key}"

    # 3) 归档库下境外实体 H1（香港）/ S1（新加坡），结构独立
    region_entity_prefix = {
        "hkg_subfolder_tokens": "归档库/H1_Hyper Horizon Limited",
        "sgp_subfolder_tokens": "归档库/S1_Spark Tide Pte. Ltd.",
    }
    for cfg_key, prefix in region_entity_prefix.items():
        for key, tk in config.get(cfg_key, {}).items():
            if not tk:
                continue
            m[tk] = prefix if key == "root" else f"{prefix}/{key}"

    # 4) 归档库下 0_CHN汇总
    for key, tk in config.get("chn_summary_tokens", {}).items():
        if not tk:
            continue
        m[tk] = "归档库/0_CHN汇总" if key == "root" else f"归档库/0_CHN汇总/{key}"

    # 5) 工作台（收件箱）
    for key, tk in config.get("inbox_tokens", {}).items():
        if tk:
            m[tk] = f"工作台/{key}"
    for inbox_key, ent_map in config.get("inbox_entity_tokens", {}).items():
        for ent, tk in ent_map.items():
            if tk:
                m[tk] = f"工作台/{inbox_key}/{ent}"

    # 6) 知识库
    for key, tk in config.get("knowledge_base_tokens", {}).items():
        if tk:
            m[tk] = f"知识库/{key}"

    # 7) 待定事项
    pending_root = config.get("root_folders", {}).get("待定事项", {}).get("folder_token", "")
    if pending_root:
        m[pending_root] = "待定事项"
    for key, tk in config.get("pending_tokens", {}).items():
        if tk:
            m[tk] = f"待定事项/{key}"

    return m


def move_file(token: str, file_token: str, dest_folder_token: str) -> dict:
    """飞书云盘内移动文件到目标文件夹。"""
    url = f"{FEISHU_BASE}/drive/v1/files/{file_token}/move"
    resp = U.feishu_request("POST", url, headers=U.get_headers(token), json={
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
    U.configure_rate_limit(settings)

    # token → 人可读完整落地路径（用于对账 CSV 的「目标末级子文件夹」列）
    token_path_map = build_token_path_map(config)

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

    # 「直达子夹」：指定了 source_prefix 时，先把前缀解析成对应子夹 token，
    # 只从该子夹往下遍历，不再陪跑整棵 staging 树。
    scan_token = staging_token
    scan_rel: tuple[str, ...] = ()
    if source_prefix:
        scan_token, scan_rel = resolve_source_subtree(token, staging_token, source_prefix)
        if scan_rel:
            print(f"直达子夹：{'/'.join(scan_rel)}（token {scan_token}）")
        else:
            print("未匹配到对应子夹，回退全树遍历后按前缀过滤")

    print("遍历 staging 文件夹…")
    staged = collect_staging_files(token, scan_token, scan_rel)
    print(f"待处理范围内共 {len(staged)} 个文件\n")

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
        if label == "fallback":
            # fallback 文件不移动，保留在原位置不动
            print(f"  跳过（fallback 保留原位）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "跳过-fallback保留原位",
                "文件token": f["file_token"], "源folder_token": f["src_folder_token"],
                "目标folder_token": "", "时间": datetime.now().isoformat(), "错误": "",
            })
            stats["skip_fallback"] = stats.get("skip_fallback", 0) + 1
            continue
        if not folder_token:
            print(f"  跳过（无目标文件夹）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "跳过-无目标",
                "文件token": f["file_token"], "源folder_token": f["src_folder_token"],
                "目标folder_token": "", "时间": datetime.now().isoformat(), "错误": "",
            })
            stats["skip_no_target"] = stats.get("skip_no_target", 0) + 1
            continue

        processed += 1

        # 目标子路径（去掉文件名那一层）
        rel_dir = sub_path.parent if sub_path != Path(".") else Path(".")
        # 飞书末级目标子文件夹（可读全路径 = folder_token 的完整落地路径 + 子目录，
        # 即文件最终落地的那一层夹，与 base 表「飞书目标文件夹」列同口径）。
        base_path = token_path_map.get(folder_token, label)
        leaf_folder = f"{base_path}/{rel_dir}" if rel_dir != Path(".") else base_path

        if dry_run:
            desc = folder_token + (f" / {rel_dir}" if rel_dir != Path(".") else "")
            # dry-run 只对已存在目标夹做查重预判（不创建子文件夹）
            dedup_hint = ""
            print(f"  [{label}] {rel_str}")
            print(f"    → 目标: {desc}{dedup_hint}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "dry-run",
                "文件token": f["file_token"], "源folder_token": f["src_folder_token"],
                "目标folder_token": folder_token, "子路径": str(sub_path),
                "目标末级子文件夹": leaf_folder,
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
                "文件token": f["file_token"], "源folder_token": f["src_folder_token"],
                "目标folder_token": folder_token, "子路径": str(sub_path),
                "目标末级子文件夹": leaf_folder,
                "时间": datetime.now().isoformat(), "错误": "无法创建目标文件夹",
            })
            stats["failed"] = stats.get("failed", 0) + 1
            continue

        # 文件名查重：目标夹已有同名文件 → 跳过
        if name in existing_names(dest_token):
            print(f"  跳过（同名已存在）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "跳过-同名",
                "文件token": f["file_token"], "源folder_token": f["src_folder_token"],
                "目标folder_token": dest_token, "子路径": str(sub_path),
                "目标末级子文件夹": leaf_folder,
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
                "文件token": f["file_token"], "源folder_token": f["src_folder_token"],
                "目标folder_token": dest_token, "子路径": str(sub_path),
                "目标末级子文件夹": leaf_folder,
                "时间": datetime.now().isoformat(), "错误": "",
            })
        else:
            err = result.get("error", "unknown") if result else "unknown"
            print(f"  失败: {rel_str} — {err}")
            stats["failed"] = stats.get("failed", 0) + 1
            log_entries.append({
                "源路径": rel_str, "路由": label, "状态": "失败",
                "文件token": f["file_token"], "源folder_token": f["src_folder_token"],
                "目标folder_token": dest_token, "子路径": str(sub_path),
                "目标末级子文件夹": leaf_folder,
                "时间": datetime.now().isoformat(), "错误": err,
            })

    # 写日志
    UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    suffix = "-dryrun" if dry_run else ""
    log_path = UPLOAD_LOG_DIR / f"move-report-{date_str}{suffix}.csv"
    fieldnames = ["源路径", "路由", "状态", "文件token", "源folder_token",
                  "目标folder_token", "子路径", "目标末级子文件夹", "时间", "错误"]
    with open(log_path, "w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(log_entries)

    failed = stats.pop("failed", 0)
    skip_dup = stats.pop("skip_dup", 0)
    skip_no_target = stats.pop("skip_no_target", 0)
    skip_fallback = stats.pop("skip_fallback", 0)
    total_ok = sum(stats.values())
    detail = " / ".join(f"{k} {v}" for k, v in sorted(stats.items()) if v > 0)

    print(f"\n{mode_label}完成：")
    print(f"  处理：{total_ok}（{detail}）")
    print(f"  同名跳过：{skip_dup}")
    print(f"  无目标跳过：{skip_no_target}")
    print(f"  fallback 保留原位：{skip_fallback}")
    if not dry_run:
        print(f"  失败：{failed}")
    print(f"  日志：{log_path}")


def run_undo(report_csv: str, dry_run: bool = False,
             staging_token: str = DEFAULT_STAGING_TOKEN):
    """撤回：读一份 move-report CSV，把其中 status=success 的文件
    从「目标夹」移回「原 staging 源夹」。

    首选走 CSV 里记录的 token（新版报告已含）——
      文件token 直接定位文件、源folder_token 直接定位原源夹，move 一步到位；
    老报告若无这两列，则退回旧法：去 目标folder_token 按文件名反查文件 token，
    并按 源路径 父目录在 staging 下解析/重建原源夹。
    幂等：目标夹里找不到该名文件（已被移回/不存在）即跳过。

    dry-run 模式只按报告规划反向移动、不触碰飞书，且额外接受 dry-run 行，
    便于拿一份 dry-run 报告演练整条 undo 流程。"""
    report_path = Path(report_csv)
    if not report_path.is_absolute():
        report_path = UPLOAD_LOG_DIR / report_path.name
    if not report_path.exists():
        print(f"找不到对账单：{report_path}")
        return

    config = U.load_config()
    settings = config.get("upload_settings", {})
    U.configure_rate_limit(settings)

    # dry-run 只做规划、不触碰飞书；正式撤回才需要 token
    token = ""
    if not dry_run:
        token = U.get_tenant_access_token()
        if not token:
            return

    # 正式撤回只认 success 行；dry-run 演练额外接受 dry-run 行
    allowed = {"success"} if not dry_run else {"success", "dry-run"}
    with open(report_path, newline="", encoding="utf-8-sig") as fp:
        rows = [r for r in csv.DictReader(fp) if r.get("状态") in allowed]

    mode_label = "undo dry-run 模拟" if dry_run else "撤回"
    print(f"模式：{mode_label}")
    print(f"对账单：{report_path}")
    print(f"待撤回（{'/'.join(sorted(allowed))} 行）：{len(rows)}\n")

    # 目标夹「文件名→token」缓存，仅老报告（无 文件token 列）反查时用
    src_name_token: dict[str, dict[str, str]] = {}

    def name_token_map(dest_token: str) -> dict[str, str]:
        if dest_token not in src_name_token:
            src_name_token[dest_token] = {
                it.get("name", ""): it.get("token", "")
                for it in U.list_folder_cached(token, dest_token)
                if it.get("type") != "folder"
            }
        return src_name_token[dest_token]

    log_entries = []
    stats = {"undone": 0, "skip_missing": 0, "failed": 0}

    for r in rows:
        rel_str = r.get("源路径", "")
        rel_parts = tuple(rel_str.split("/"))
        name = rel_parts[-1]
        current_parent = r.get("目标folder_token", "")
        rec_file_token = r.get("文件token", "")      # 新版报告直接记录，优先用
        rec_src_folder = r.get("源folder_token", "")  # 新版报告直接记录，优先用
        if not current_parent or not name:
            continue

        # 解析原源夹（源路径父目录，相对 staging 根），供无记录时回退
        rel_dir = Path(*rel_parts[:-1]) if len(rel_parts) > 1 else Path(".")

        if dry_run:
            # 只规划、不触碰飞书：优先展示记录的源夹 token，否则展示重建路径
            back_to = rec_src_folder or f"staging/{rel_dir if rel_dir != Path('.') else ''}"
            print(f"  [undo] {rel_str}")
            print(f"    {current_parent} → {back_to}")
            log_entries.append({
                "源路径": rel_str, "状态": "undo-dry-run",
                "从folder_token": current_parent, "回folder_token": rec_src_folder,
                "时间": datetime.now().isoformat(), "错误": "",
            })
            stats["undone"] += 1
            continue

        # 文件 token：优先用报告记录，缺失才回退到目标夹按文件名反查
        file_token = rec_file_token or name_token_map(current_parent).get(name, "")
        if not file_token:
            print(f"  跳过（目标夹已无此文件，可能已撤回）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "状态": "跳过-目标夹无此文件",
                "从folder_token": current_parent, "回folder_token": "",
                "时间": datetime.now().isoformat(), "错误": "",
            })
            stats["skip_missing"] += 1
            continue

        # 原源夹 token：优先用报告记录，缺失才回退到按路径重建
        if rec_src_folder:
            src_token = rec_src_folder
        elif rel_dir != Path("."):
            src_token = U.ensure_folder_path(token, staging_token, rel_dir)
        else:
            src_token = staging_token
        if not src_token:
            print(f"  失败（无法定位/重建原源夹）: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "状态": "失败",
                "从folder_token": current_parent, "回folder_token": "",
                "时间": datetime.now().isoformat(), "错误": "无法定位原源夹",
            })
            stats["failed"] += 1
            continue

        result = None
        for _ in range(settings.get("retry_max", 3)):
            result = move_file(token, file_token, src_token)
            if result["status"] == "success":
                break
            time.sleep(settings.get("retry_delay_seconds", 5))

        if result and result["status"] == "success":
            # 从目标夹缓存移除，避免同名误判（仅反查路径下有意义）
            if current_parent in src_name_token:
                src_name_token[current_parent].pop(name, None)
            print(f"  [undo] 移回: {rel_str}")
            log_entries.append({
                "源路径": rel_str, "状态": "success",
                "从folder_token": current_parent, "回folder_token": src_token,
                "时间": datetime.now().isoformat(), "错误": "",
            })
            stats["undone"] += 1
        else:
            err = result.get("error", "unknown") if result else "unknown"
            print(f"  失败: {rel_str} — {err}")
            log_entries.append({
                "源路径": rel_str, "状态": "失败",
                "从folder_token": current_parent, "回folder_token": src_token,
                "时间": datetime.now().isoformat(), "错误": err,
            })
            stats["failed"] += 1

    UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    suffix = "-dryrun" if dry_run else ""
    log_path = UPLOAD_LOG_DIR / f"undo-report-{date_str}{suffix}.csv"
    fieldnames = ["源路径", "状态", "从folder_token", "回folder_token", "时间", "错误"]
    with open(log_path, "w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(log_entries)

    print(f"\n{mode_label}完成：")
    print(f"  撤回：{stats['undone']}")
    print(f"  跳过（目标夹已无）：{stats['skip_missing']}")
    if not dry_run:
        print(f"  失败：{stats['failed']}")
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
    parser.add_argument("--undo", type=str, default=None, metavar="MOVE_REPORT_CSV",
                        help="撤回模式：读一份 move-report CSV，把 success 行移回原源夹")
    args = parser.parse_args()
    if args.undo:
        run_undo(report_csv=args.undo, dry_run=args.dry_run, staging_token=args.staging)
    else:
        run_move(dry_run=args.dry_run, staging_token=args.staging,
                 source_prefix=args.source_prefix, limit=args.limit)


if __name__ == "__main__":
    main()
