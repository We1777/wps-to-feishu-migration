#!/usr/bin/env python3
"""将03-staging中已整理的文件上传到飞书云盘"""

import os
import csv
import json
import time
import hashlib
import requests
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "00-config" / "target-space.json"
STAGING_DIR = PROJECT_ROOT / "03-staging"
UPLOAD_LOG_DIR = PROJECT_ROOT / "04-upload-logs"
ERROR_QUEUE = UPLOAD_LOG_DIR / "error-retry-queue.txt"

FEISHU_BASE = "https://open.feishu.cn/open-apis"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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


def get_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def create_folder(token: str, parent_token: str, name: str) -> str | None:
    url = f"{FEISHU_BASE}/drive/v1/files/create_folder"
    resp = requests.post(url, headers=get_headers(token), json={
        "name": name,
        "folder_token": parent_token,
    })
    data = resp.json()
    if data.get("code") == 0:
        return data["data"]["token"]
    print(f"  创建文件夹失败 [{name}]: {data.get('msg', resp.text)}")
    return None


def ensure_folder_path(token: str, root_token: str, rel_path: Path) -> str | None:
    current_token = root_token
    for part in rel_path.parts:
        url = f"{FEISHU_BASE}/drive/v1/files"
        resp = requests.get(url, headers=get_headers(token), params={
            "folder_token": current_token,
            "page_size": 200,
        })
        data = resp.json()
        found = None
        if data.get("code") == 0:
            for item in data.get("data", {}).get("files", []):
                if item.get("name") == part and item.get("type") == "folder":
                    found = item["token"]
                    break

        if found:
            current_token = found
        else:
            new_token = create_folder(token, current_token, part)
            if not new_token:
                return None
            current_token = new_token

    return current_token


def upload_file(token: str, folder_token: str, filepath: Path) -> dict:
    url = f"{FEISHU_BASE}/drive/v1/files/upload_all"
    file_size = filepath.stat().st_size

    with open(filepath, "rb") as f:
        resp = requests.post(url, headers=get_headers(token), data={
            "file_name": filepath.name,
            "parent_type": "explorer",
            "parent_node": folder_token,
            "size": str(file_size),
        }, files={"file": (filepath.name, f)})

    data = resp.json()
    if data.get("code") == 0:
        return {"status": "success", "file_token": data["data"]["file_token"]}
    return {"status": "failed", "error": data.get("msg", resp.text)}


def md5_hash(filepath: Path) -> str:
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def run_upload():
    config = load_config()
    root_folders = config["root_folders"]
    settings = config["upload_settings"]

    token = get_tenant_access_token()
    if not token:
        print("错误：无法获取飞书access_token，请检查环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return

    date_str = datetime.now().strftime("%Y%m%d")
    log_path = UPLOAD_LOG_DIR / f"upload-report-{date_str}.csv"
    UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_entries = []
    errors = []
    stats = {"success": 0, "failed": 0, "skipped": 0}

    for top_dir in ["会计档案", "非会计档案"]:
        top_path = STAGING_DIR / top_dir
        if not top_path.exists():
            continue

        root_token = root_folders.get(top_dir, {}).get("folder_token", "")
        if not root_token:
            print(f"警告：{top_dir}未配置folder_token，跳过")
            continue

        for root, _, filenames in os.walk(top_path):
            for fname in filenames:
                if fname.startswith("."):
                    continue

                src = Path(root) / fname
                rel = src.relative_to(top_path)
                rel_dir = rel.parent

                folder_token = ensure_folder_path(token, root_token, rel_dir)
                if not folder_token:
                    entry = {
                        "源路径": str(src.relative_to(STAGING_DIR)),
                        "状态": "失败",
                        "目标URL": "",
                        "文件token": "",
                        "MD5": "",
                        "时间": datetime.now().isoformat(),
                        "错误": "无法创建目标文件夹",
                    }
                    log_entries.append(entry)
                    errors.append(str(src.relative_to(STAGING_DIR)))
                    stats["failed"] += 1
                    continue

                result = None
                for attempt in range(settings.get("retry_max", 3)):
                    result = upload_file(token, folder_token, src)
                    if result["status"] == "success":
                        break
                    time.sleep(settings.get("retry_delay_seconds", 5))

                file_md5 = md5_hash(src)
                entry = {
                    "源路径": str(src.relative_to(STAGING_DIR)),
                    "状态": result["status"],
                    "目标URL": "",
                    "文件token": result.get("file_token", ""),
                    "MD5": file_md5,
                    "时间": datetime.now().isoformat(),
                    "错误": result.get("error", ""),
                }
                log_entries.append(entry)

                if result["status"] == "success":
                    stats["success"] += 1
                else:
                    stats["failed"] += 1
                    errors.append(str(src.relative_to(STAGING_DIR)))

    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["源路径", "状态", "目标URL", "文件token", "MD5", "时间", "错误"])
        writer.writeheader()
        writer.writerows(log_entries)

    if errors:
        with open(ERROR_QUEUE, "w", encoding="utf-8") as f:
            f.write("\n".join(errors))

    print(f"上传完成：")
    print(f"  成功：{stats['success']}，失败：{stats['failed']}，跳过：{stats['skipped']}")
    print(f"  日志：{log_path}")
    if errors:
        print(f"  失败文件清单：{ERROR_QUEUE}")


if __name__ == "__main__":
    run_upload()
