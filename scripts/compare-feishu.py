#!/usr/bin/env python3
"""比对scan-report.csv与飞书云盘实际文件，输出差异报告
支持7万+文件量：并发遍历 + 断点续传 + O(n)字典匹配"""

import os
import csv
import json
import time
import requests
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "00-config" / "target-space.json"
SCAN_REPORT = PROJECT_ROOT / "01-source-audit" / "scan-report.csv"
COMPARE_REPORT = PROJECT_ROOT / "05-verification" / "feishu-compare-report.csv"
SUMMARY_REPORT = PROJECT_ROOT / "05-verification" / "feishu-compare-summary.txt"
CHECKPOINT_FILE = PROJECT_ROOT / "05-verification" / "feishu-scan-checkpoint.json"

FEISHU_BASE = "https://open.feishu.cn/open-apis"
MAX_WORKERS = 5
MAX_RETRIES = 3
RETRY_DELAY = 2


def get_tenant_access_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        print("错误：请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return ""
    resp = requests.post(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    data = resp.json()
    if data.get("code") == 0:
        return data["tenant_access_token"]
    print(f"错误：获取tenant_access_token失败: {data.get('msg', resp.text)}")
    return ""


def api_request_with_retry(url: str, headers: dict, params: dict) -> dict | None:
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            data = resp.json()
            if data.get("code") == 0:
                return data
            if data.get("code") == 99991400:
                wait = RETRY_DELAY * (attempt + 1)
                print(f"    API限流，等待{wait}秒后重试...")
                time.sleep(wait)
                continue
            print(f"    API错误 [{data.get('code')}]: {data.get('msg', '')}")
            return None
        except requests.exceptions.Timeout:
            print(f"    请求超时，重试 {attempt + 1}/{MAX_RETRIES}")
            time.sleep(RETRY_DELAY)
        except Exception as e:
            print(f"    请求异常: {e}")
            return None
    return None


def list_folder_flat(token: str, folder_token: str, prefix: str) -> tuple[list[dict], list[tuple]]:
    """遍历单个文件夹（不递归），返回文件列表和子文件夹列表"""
    files = []
    subfolders = []
    page_token = None
    headers = {"Authorization": f"Bearer {token}"}

    while True:
        params = {"folder_token": folder_token, "page_size": 200}
        if page_token:
            params["page_token"] = page_token

        data = api_request_with_retry(
            f"{FEISHU_BASE}/drive/v1/files", headers, params
        )
        if not data:
            break

        for item in data.get("data", {}).get("files", []):
            name = item.get("name", "")
            item_type = item.get("type", "")
            rel_path = f"{prefix}/{name}" if prefix else name

            if item_type == "folder":
                subfolders.append((item["token"], rel_path))
            else:
                files.append({
                    "文件名": name,
                    "飞书路径": rel_path,
                    "飞书token": item.get("token", ""),
                    "大小_字节": int(item.get("size", 0)) if item.get("size") else 0,
                    "类型": item.get("type", ""),
                })

        if not data.get("data", {}).get("has_more", False):
            break
        page_token = data["data"].get("page_token")

    return files, subfolders


def list_feishu_files_concurrent(token: str, root_folders: dict) -> list[dict]:
    """并发BFS遍历飞书云盘所有文件夹"""
    all_files = []
    files_lock = Lock()
    scanned_count = [0]

    checkpoint = load_checkpoint()
    scanned_tokens = set(checkpoint.get("scanned_tokens", []))
    if scanned_tokens:
        all_files = checkpoint.get("files", [])
        print(f"  从断点恢复：已扫描 {len(scanned_tokens)} 个文件夹，{len(all_files)} 个文件")

    pending_folders = []
    for folder_name, folder_conf in root_folders.items():
        folder_token = folder_conf.get("folder_token", "")
        if not folder_token:
            print(f"  跳过 {folder_name}：未配置folder_token")
            continue
        if folder_token not in scanned_tokens:
            pending_folders.append((folder_token, folder_name))

    def process_folder(folder_token: str, prefix: str) -> tuple[list[dict], list[tuple]]:
        if folder_token in scanned_tokens:
            return [], []
        files, subfolders = list_folder_flat(token, folder_token, prefix)
        with files_lock:
            scanned_tokens.add(folder_token)
            scanned_count[0] += 1
            if scanned_count[0] % 50 == 0:
                print(f"    已扫描 {scanned_count[0]} 个文件夹，累计 {len(all_files) + len(files)} 个文件...")
                save_checkpoint(list(scanned_tokens), all_files)
        return files, subfolders

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while pending_folders:
            futures = {}
            for ft, prefix in pending_folders:
                futures[executor.submit(process_folder, ft, prefix)] = (ft, prefix)

            pending_folders = []
            for future in as_completed(futures):
                try:
                    files, subfolders = future.result()
                    with files_lock:
                        all_files.extend(files)
                    for sf_token, sf_prefix in subfolders:
                        if sf_token not in scanned_tokens:
                            pending_folders.append((sf_token, sf_prefix))
                except Exception as e:
                    ft, prefix = futures[future]
                    print(f"    处理文件夹失败 [{prefix}]: {e}")

    save_checkpoint(list(scanned_tokens), all_files)
    return all_files


def load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_checkpoint(scanned_tokens: list, files: list):
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "scanned_tokens": scanned_tokens,
            "files": files,
            "saved_at": datetime.now().isoformat(),
        }, f, ensure_ascii=False)


def clear_checkpoint():
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()


def load_scan_report() -> list[dict]:
    if not SCAN_REPORT.exists():
        print(f"错误：未找到 {SCAN_REPORT}，请先运行 scan-source.py")
        return []
    rows = []
    with open(SCAN_REPORT, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def compare():
    start_time = time.time()
    print("加载scan-report.csv...")
    scan_rows = load_scan_report()
    if not scan_rows:
        return

    print(f"  源端文件总数：{len(scan_rows)}")

    if not CONFIG_PATH.exists():
        print(f"错误：未找到 {CONFIG_PATH}，请先配置target-space.json")
        return

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    token = get_tenant_access_token()
    if not token:
        return

    print(f"并发扫描飞书云盘（{MAX_WORKERS}线程）...")
    feishu_files = list_feishu_files_concurrent(token, config["root_folders"])
    scan_time = time.time() - start_time
    print(f"  飞书端文件总数：{len(feishu_files)}（扫描耗时 {scan_time:.0f}秒）")

    print("比对中...")
    feishu_by_name: dict[str, list[dict]] = {}
    for ff in feishu_files:
        feishu_by_name.setdefault(ff["文件名"], []).append(ff)

    scan_by_name: dict[str, list[dict]] = {}
    for sr in scan_rows:
        scan_by_name.setdefault(sr["文件名"], []).append(sr)

    results = []
    stats = {
        "名称+大小一致": 0,
        "名称一致_大小不同": 0,
        "源端有_飞书缺失": 0,
        "飞书有_源端无记录": 0,
    }

    for fname, src_list in scan_by_name.items():
        if fname in feishu_by_name:
            feishu_list = feishu_by_name[fname]
            for src in src_list:
                src_size = int(src.get("大小_字节", 0))
                size_matched = any(
                    abs(ff["大小_字节"] - src_size) <= 1024
                    for ff in feishu_list
                )
                if size_matched:
                    status = "一致"
                    stats["名称+大小一致"] += 1
                else:
                    status = "大小不一致"
                    stats["名称一致_大小不同"] += 1

                feishu_paths = "; ".join(ff["飞书路径"] for ff in feishu_list)
                feishu_sizes = "; ".join(str(ff["大小_字节"]) for ff in feishu_list)

                results.append({
                    "文件名": fname,
                    "源端路径": src.get("相对路径", ""),
                    "源端大小": src_size,
                    "源端MD5": src.get("MD5", ""),
                    "飞书路径": feishu_paths,
                    "飞书大小": feishu_sizes,
                    "校验结果": status,
                })
        else:
            for src in src_list:
                stats["源端有_飞书缺失"] += 1
                results.append({
                    "文件名": fname,
                    "源端路径": src.get("相对路径", ""),
                    "源端大小": int(src.get("大小_字节", 0)),
                    "源端MD5": src.get("MD5", ""),
                    "飞书路径": "",
                    "飞书大小": "",
                    "校验结果": "飞书缺失",
                })

    for fname, feishu_list in feishu_by_name.items():
        if fname not in scan_by_name:
            for ff in feishu_list:
                stats["飞书有_源端无记录"] += 1
                results.append({
                    "文件名": fname,
                    "源端路径": "",
                    "源端大小": "",
                    "源端MD5": "",
                    "飞书路径": ff["飞书路径"],
                    "飞书大小": ff["大小_字节"],
                    "校验结果": "源端无记录（飞书多余）",
                })

    COMPARE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(COMPARE_REPORT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "文件名", "源端路径", "源端大小", "源端MD5",
            "飞书路径", "飞书大小", "校验结果",
        ])
        writer.writeheader()
        writer.writerows(results)

    total_time = time.time() - start_time
    summary_lines = [
        f"飞书云盘比对报告 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 50,
        f"源端文件总数：{len(scan_rows)}",
        f"飞书端文件总数：{len(feishu_files)}",
        f"飞书扫描耗时：{scan_time:.0f}秒",
        f"总耗时：{total_time:.0f}秒",
        "",
        "比对结果：",
        f"  名称+大小一致：{stats['名称+大小一致']}",
        f"  名称一致/大小不同：{stats['名称一致_大小不同']}",
        f"  源端有/飞书缺失：{stats['源端有_飞书缺失']}",
        f"  飞书有/源端无记录：{stats['飞书有_源端无记录']}",
    ]

    if stats["源端有_飞书缺失"] > 0:
        summary_lines.append("")
        summary_lines.append("缺失文件清单：")
        missing = [r for r in results if r["校验结果"] == "飞书缺失"]
        for r in missing[:500]:
            summary_lines.append(f"  - {r['源端路径']}")
        if len(missing) > 500:
            summary_lines.append(f"  ...（共{len(missing)}个，仅显示前500个）")

    with open(SUMMARY_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))

    clear_checkpoint()

    print(f"\n比对完成（总耗时 {total_time:.0f}秒）：")
    print(f"  名称+大小一致：{stats['名称+大小一致']}")
    print(f"  名称一致/大小不同：{stats['名称一致_大小不同']}")
    print(f"  源端有/飞书缺失：{stats['源端有_飞书缺失']}")
    print(f"  飞书有/源端无记录：{stats['飞书有_源端无记录']}")
    print(f"\n  详细报告：{COMPARE_REPORT}")
    print(f"  摘要报告：{SUMMARY_REPORT}")

    if stats["源端有_飞书缺失"] > 0 or stats["名称一致_大小不同"] > 0:
        print("\n存在差异，请检查报告！")
        return False
    print("\n全部文件比对通过。")
    return True


if __name__ == "__main__":
    compare()
