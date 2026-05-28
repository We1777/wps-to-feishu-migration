#!/usr/bin/env python3
"""比对scan-report.csv与飞书云盘实际文件，输出差异报告"""

import os
import csv
import json
import requests
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "00-config" / "target-space.json"
SCAN_REPORT = PROJECT_ROOT / "01-source-audit" / "scan-report.csv"
COMPARE_REPORT = PROJECT_ROOT / "05-verification" / "feishu-compare-report.csv"
SUMMARY_REPORT = PROJECT_ROOT / "05-verification" / "feishu-compare-summary.txt"

FEISHU_BASE = "https://open.feishu.cn/open-apis"


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


def list_feishu_files(token: str, folder_token: str, prefix: str = "") -> list[dict]:
    """递归遍历飞书云盘文件夹，返回所有文件信息"""
    files = []
    page_token = None

    while True:
        params = {"folder_token": folder_token, "page_size": 200}
        if page_token:
            params["page_token"] = page_token

        resp = requests.get(
            f"{FEISHU_BASE}/drive/v1/files",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        data = resp.json()

        if data.get("code") != 0:
            print(f"  警告：读取文件夹失败 [{folder_token}]: {data.get('msg', resp.text)}")
            break

        for item in data.get("data", {}).get("files", []):
            name = item.get("name", "")
            item_type = item.get("type", "")
            rel_path = f"{prefix}/{name}" if prefix else name

            if item_type == "folder":
                sub_files = list_feishu_files(token, item["token"], rel_path)
                files.extend(sub_files)
            else:
                files.append({
                    "文件名": name,
                    "飞书路径": rel_path,
                    "飞书token": item.get("token", ""),
                    "大小_字节": int(item.get("size", 0)) if item.get("size") else 0,
                    "类型": item.get("type", ""),
                    "创建时间": item.get("created_time", ""),
                    "修改时间": item.get("modified_time", ""),
                })

        if not data.get("data", {}).get("has_more", False):
            break
        page_token = data["data"].get("page_token")

    return files


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
    print("加载scan-report.csv...")
    scan_rows = load_scan_report()
    if not scan_rows:
        return

    print(f"  源端文件总数：{len(scan_rows)}")

    config_path = CONFIG_PATH
    if not config_path.exists():
        print(f"错误：未找到 {config_path}，请先配置target-space.json")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    token = get_tenant_access_token()
    if not token:
        return

    print("扫描飞书云盘文件...")
    feishu_files = []
    for folder_name, folder_conf in config["root_folders"].items():
        folder_token = folder_conf.get("folder_token", "")
        if not folder_token:
            print(f"  跳过 {folder_name}：未配置folder_token")
            continue
        print(f"  扫描 {folder_name} ({folder_token})...")
        sub_files = list_feishu_files(token, folder_token, folder_name)
        feishu_files.extend(sub_files)
        print(f"    发现 {len(sub_files)} 个文件")

    print(f"  飞书端文件总数：{len(feishu_files)}")

    # 建立飞书端文件名索引（文件名 -> 列表，因为可能有同名文件在不同目录）
    feishu_by_name: dict[str, list[dict]] = {}
    for ff in feishu_files:
        feishu_by_name.setdefault(ff["文件名"], []).append(ff)

    # 建立源端文件名索引
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

    # 对比：源端 -> 飞书端
    matched_feishu_names = set()
    for fname, src_list in scan_by_name.items():
        if fname in feishu_by_name:
            matched_feishu_names.add(fname)
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

    # 飞书端多余文件
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

    # 输出CSV报告
    COMPARE_REPORT.parent.mkdir(parents=True, exist_ok=True)
    with open(COMPARE_REPORT, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "文件名", "源端路径", "源端大小", "源端MD5",
            "飞书路径", "飞书大小", "校验结果",
        ])
        writer.writeheader()
        writer.writerows(results)

    # 输出摘要
    summary_lines = [
        f"飞书云盘比对报告 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 50,
        f"源端文件总数：{len(scan_rows)}",
        f"飞书端文件总数：{len(feishu_files)}",
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
        for r in results:
            if r["校验结果"] == "飞书缺失":
                summary_lines.append(f"  - {r['源端路径']}")

    with open(SUMMARY_REPORT, "w", encoding="utf-8") as f:
        f.write("\n".join(summary_lines))

    print(f"\n比对完成：")
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
