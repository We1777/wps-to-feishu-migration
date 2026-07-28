#!/usr/bin/env python3
"""从02-downloads直接上传到飞书云盘，19条路由规则：
  1-3.  80.x 会计电子档案 → 按关键词细分到实体 a/b/c/d 子文件夹
  4-5.  1.1 APAC集团 → HKG/SGP 地区文件夹
  6-11. 集团框架外主体 → CHN归档/收件箱（按公司+子类分流）
  12-14.美国公司/Token → USA工作台文件夹
  15.   2.0 收入管理 → 按公司分流到会计凭证/其他会计资料
  16-19.知识库直传（公司用小程序/Sharing/Issues/Agent）
"""

import os
import sys
import csv
import json
import time
import fcntl
import hashlib
import argparse
import requests
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "00-config" / "target-space.json"
MAPPING_PATH = PROJECT_ROOT / "00-config" / "wps-folder-mapping.json"
DOWNLOADS_DIR = PROJECT_ROOT / "02-downloads"
UPLOAD_LOG_DIR = PROJECT_ROOT / "04-upload-logs"
ERROR_QUEUE = UPLOAD_LOG_DIR / "error-retry-queue.txt"

FEISHU_BASE = "https://open.feishu.cn/open-apis"

ARCHIVE_CATEGORY_KEYWORDS = {
    "a. 财务会计报告": ["财务报告", "财务报表", "审计报告", "年报", "季报", "月报",
                        "年度", "季度", "月度", "annual", "quarterly", "monthly"],
    "b. 会计账簿": ["总账", "明细账", "日记账", "辅助账", "账簿", "ledger", "journal"],
    "c. 会计凭证": ["凭证", "原始凭证", "记账凭证", "voucher",
                     "发票", "账单", "回单", "增票", "水单", "付款"],
    "d. 其他会计资料": ["对账单", "纳税申报", "收入单据", "收入合同", "银行对账", "内审", "其他"],
}

VOUCHER_SUBCATEGORY_KEYWORDS = {
    "a. 对公付款 - 账单": ["对公付款", "账单", "付款水单", "付款资料", "RECEIPT"],
    "b. 国内发票": ["增值税普通发票", "增值税专用发票", "VATNR", "VATSP",
                     "VATSPIN", "VATNRIN", "VATINNR", "INVINNR", "国内发票"],
    "c. 收入增票": ["收入增票", "销售发票", "VATOTNR", "CMINVOT", "VATSPOT", "VATNROT"],
    "d. 境外发票": ["境外发票", "商业发票", "CMINVIN", "overseas", "foreign",
                     "commercial invoice"],
    "e. 内部凭证": ["内部凭证", "内部转账", "记账凭证", "INTLSPT"],
    "f. 税局回单 - 人资相关": ["社保", "公积金", "个税", "人资", "SSTPYMT", "社保公积金"],
    "g. 税局回单 - 纳税回单": ["纳税", "完税", "印花税", "税费", "税局", "TAXPYMT", "TAXCLMX"],
    "h. 第三方回单": ["第三方", "银行手续费", "手续费", "BANKBLL"],
}


# ── 跨进程排他写锁 ──────────────────────────────────────────────────────────
# 重复夹的根因是「两个写进程并发跑」：分段重跑时上一段进程尚未结束（或刚建的夹
# 还在飞书 read-after-write 延迟窗口内未进列表），另一段进程的 create_folder 建前
# 复查看不到 → 又建一个同名夹。create_folder 现已幂等，唯一残余风险就是并发。
# 用 flock 排他锁把所有「建夹/移动/合并」进程串行化：同一时刻只允许一个写进程。
# flock 在进程退出/崩溃时由内核自动释放，不会留下需手工清理的残留锁。
WRITE_LOCK_PATH = UPLOAD_LOG_DIR / ".feishu-write.lock"


class WriteLockHeld(Exception):
    """另一个飞书写进程（移动/合并）正持有排他锁，本进程应拒绝启动而非并发。"""


def acquire_write_lock(label: str = ""):
    """获取跨进程排他写锁。成功返回已加锁的文件对象（须在结束时 release_write_lock）；
    已被占用则抛 WriteLockHeld，异常里带持锁者信息（pid/label/时间）。"""
    UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)
    fp = open(WRITE_LOCK_PATH, "a+")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fp.seek(0)
        holder = fp.read().strip() or "(未知)"
        fp.close()
        raise WriteLockHeld(holder)
    # 拿到锁 → 覆盖写入本进程信息，供并发者知道是谁在跑
    fp.seek(0)
    fp.truncate()
    fp.write(f"pid={os.getpid()} label={label} at={datetime.now().isoformat()}\n")
    fp.flush()
    return fp


def release_write_lock(fp) -> None:
    if fp is None:
        return
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    finally:
        fp.close()


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_folder_mapping() -> list[dict]:
    if not MAPPING_PATH.exists():
        return []
    with open(MAPPING_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("mappings", [])


def get_tenant_access_token() -> str:
    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        print("错误：请设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return ""
    resp = feishu_request(
        "POST",
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


# ---- 限流 + 429/频控指数退避（upload 与 move 共用同一道闸）----
_last_call_ts = 0.0
_min_interval = 0.25          # ≤4 QPS，留缓冲；configure_rate_limit 可覆盖
_rate_limit_retry_max = 5     # 被限流后的最大重试次数
_rate_limit_base_delay = 5.0  # 无 Retry-After 头时的指数退避基数（秒）

# 飞书接口层频控错误码（HTTP 429 之外，body 里返回的限流 code）
FEISHU_RATE_LIMIT_CODES = {99991400}


def configure_rate_limit(settings: dict) -> None:
    """从 upload_settings 读取限流参数（可选），否则用默认值。"""
    global _min_interval, _rate_limit_retry_max, _rate_limit_base_delay
    qps = settings.get("max_qps", 4)
    if qps and qps > 0:
        _min_interval = 1.0 / qps
    _rate_limit_retry_max = settings.get("rate_limit_retry_max", 5)
    _rate_limit_base_delay = settings.get("retry_delay_seconds", 5)


def _throttle() -> None:
    """调用间主动限速：保证相邻两次 API 调用间隔 ≥ _min_interval，从源头压在阈值下。"""
    global _last_call_ts
    wait = _min_interval - (time.monotonic() - _last_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_call_ts = time.monotonic()


def _rewind_files(files) -> None:
    """退避重试前把文件体指针复位到 0，避免二次请求读到空内容。"""
    if not files:
        return
    for v in files.values():
        fobj = v[1] if isinstance(v, (list, tuple)) and len(v) > 1 else v
        if hasattr(fobj, "seek"):
            try:
                fobj.seek(0)
            except (OSError, ValueError):
                pass


def feishu_request(method: str, url: str, **kwargs) -> "requests.Response":
    """统一的飞书 API 调用入口：调用间限流（≤max_qps）+ HTTP 429/频控码指数退避。
    命中限流时优先读 Retry-After 头，否则按 base*2^n 退避；非限流响应原样返回，
    调用方沿用原有的 code==0 判断与业务重试逻辑。"""
    attempt = 0
    while True:
        _throttle()
        _rewind_files(kwargs.get("files"))
        resp = requests.request(method, url, **kwargs)

        limited = resp.status_code == 429
        if not limited and resp.status_code < 400:
            try:
                limited = resp.json().get("code") in FEISHU_RATE_LIMIT_CODES
            except ValueError:
                limited = False

        if not limited or attempt >= _rate_limit_retry_max:
            return resp

        retry_after = resp.headers.get("Retry-After", "")
        if retry_after.isdigit():
            delay = float(retry_after)
        else:
            delay = _rate_limit_base_delay * (2 ** attempt)
        print(f"  触发飞书频控（{resp.status_code}），{delay:.0f}s 后重试…")
        time.sleep(delay)
        attempt += 1


folder_list_cache: dict[str, list] = {}


def list_folder_cached(token: str, folder_token: str) -> list:
    if folder_token in folder_list_cache:
        return folder_list_cache[folder_token]
    url = f"{FEISHU_BASE}/drive/v1/files"
    all_files = []
    page_token = ""
    while True:
        params = {"folder_token": folder_token, "page_size": 200}
        if page_token:
            params["page_token"] = page_token
        resp = feishu_request("GET", url, headers=get_headers(token), params=params)
        data = resp.json()
        if data.get("code") != 0:
            break
        all_files.extend(data.get("data", {}).get("files", []))
        page_token = data.get("data", {}).get("next_page_token", "")
        if not page_token:
            break
    folder_list_cache[folder_token] = all_files
    return all_files


def list_folder_live(token: str, folder_token: str) -> list:
    """绕过缓存、实时 GET 父夹子项后回填缓存。用于建夹前复查，规避飞书
    read-after-write 延迟（刚建的夹尚未进列表）。"""
    folder_list_cache.pop(folder_token, None)
    return list_folder_cached(token, folder_token)


def create_folder(token: str, parent_token: str, name: str) -> str | None:
    """幂等建夹，根治「re-list 未见刚建夹 → 重复建同名夹」竞态：
    1) 建前实时复查父夹——若已有同名子夹（含上一次 create 因 read-after-write 延迟
       尚未进缓存、或跨 run/进程已建的情况），直接复用其 token，绝不重复创建；
    2) 确无同名才创建；成功后把新夹并入父夹缓存，后续同 run 查找直接命中、不再 re-list，
       从源头消除延迟窗口。"""
    # 1) 建前实时复查，命中同名夹即复用
    for item in list_folder_live(token, parent_token):
        if item.get("name") == name and item.get("type") == "folder":
            return item["token"]
    # 2) 确无 → 创建
    url = f"{FEISHU_BASE}/drive/v1/files/create_folder"
    resp = feishu_request("POST", url, headers=get_headers(token), json={
        "name": name,
        "folder_token": parent_token,
    })
    data = resp.json()
    if data.get("code") == 0:
        new_token = data["data"]["token"]
        # 把新夹并入父夹缓存，避免下一个文件 re-list 时因延迟看不到而重复建
        entry = {"name": name, "type": "folder", "token": new_token}
        cache = folder_list_cache.get(parent_token)
        if cache is not None:
            cache.append(entry)
        else:
            folder_list_cache[parent_token] = [entry]
        return new_token
    print(f"  创建文件夹失败 [{name}]: {data.get('msg', resp.text)}")
    return None


def ensure_folder_path(token: str, root_token: str, rel_path: Path) -> str | None:
    current_token = root_token
    for part in rel_path.parts:
        children = list_folder_cached(token, current_token)
        found = None
        for item in children:
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
        resp = feishu_request("POST", url, headers=get_headers(token), data={
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


def resolve_chn_entity(path_parts: tuple[str, ...], config: dict) -> tuple[str | None, int | None]:
    keywords = config.get("chn_entity_keywords", {})
    for i, part in enumerate(path_parts):
        for kw, entity in keywords.items():
            if kw in part:
                return entity, i
    return None, None


def resolve_entity_token(entity: str, subpath: str, config: dict) -> str | None:
    tokens = config.get("entity_subfolder_tokens", {}).get(entity, {})
    return tokens.get(subpath)


def detect_archive_category(path_parts: tuple[str, ...]) -> str | None:
    text = "/".join(path_parts).lower()
    for cat_name, keywords in ARCHIVE_CATEGORY_KEYWORDS.items():
        if cat_name.split(". ")[0] + "." in text:
            return cat_name
        for kw in keywords:
            if kw.lower() in text:
                return cat_name
    return None


def detect_voucher_subcategory(path_parts: tuple[str, ...]) -> str | None:
    text = "/".join(path_parts)
    for sub_name, keywords in VOUCHER_SUBCATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return sub_name
    return None


def extract_fiscal_year(path_parts: tuple[str, ...]) -> str | None:
    import re
    text = "/".join(path_parts)
    m = re.search(r"FY-(\d{4})", text)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4})", text)
    if m:
        year = int(m.group(1))
        if 2020 <= year <= 2099:
            return str(year)
    return None


def resolve_inbox_entity_token(inbox_key: str, entity_kw: str, config: dict) -> str | None:
    entity_map = config.get("inbox_entity_tokens", {}).get(inbox_key, {})
    for kw, tk in entity_map.items():
        if kw in entity_kw:
            return tk
    return None


_folder_mapping: list[dict] = []


def classify_by_mapping(rel_parts: tuple[str, ...], config: dict) -> tuple[str, str, Path] | None:
    """用 wps-folder-mapping.json 精确匹配 WPS 源路径 → 飞书目标路径。
    返回 (route_label, root_folder_token, sub_path) 或 None（无匹配）。
    映射表按路径长度降序排列，最长前缀优先匹配。"""
    global _folder_mapping
    if not _folder_mapping:
        _folder_mapping = load_folder_mapping()
    if not _folder_mapping:
        return None

    rel_str = "/".join(rel_parts)
    for entry in _folder_mapping:
        wps_src = entry["wps_source"]
        # base 表个别行（如 99.0 待上传GDrive资料）带 "财务部/" 根前缀，
        # 而 staging 实际相对路径已剥离该层，剥前缀对齐后再比对。
        if wps_src.startswith("财务部/"):
            wps_src = wps_src[len("财务部/"):]
        if rel_str.lower().startswith(wps_src.lower()):
            feishu_path = entry["feishu_path"]
            remaining = rel_str[len(wps_src):].lstrip("/")

            region = feishu_path[0] if feishu_path else ""
            entity = feishu_path[1] if len(feishu_path) > 1 else ""

            root_token = ""
            entity_tokens = config.get("entity_subfolder_tokens", {})
            for ent_key, ent_data in entity_tokens.items():
                if entity and entity.startswith(ent_key):
                    root_token = ent_data.get("root", "")
                    subfolder_path = "/".join(feishu_path[2:])
                    for path_key in sorted(ent_data.keys(), key=len, reverse=True):
                        if path_key != "root" and subfolder_path.startswith(path_key):
                            root_token = ent_data[path_key]
                            subfolder_path = subfolder_path[len(path_key):].lstrip("/")
                            break
                    if subfolder_path:
                        sub_parts = subfolder_path.split("/") + (remaining.split("/") if remaining else [])
                        sub_parts = [p for p in sub_parts if p]
                    else:
                        sub_parts = remaining.split("/") if remaining else []
                        sub_parts = [p for p in sub_parts if p]
                    sub = Path(*sub_parts) if sub_parts else Path(".")
                    return f"精确映射({entity})", root_token, sub

            # 归档库 下的 APAC 境外实体（H1_ 香港 / S1_ 新加坡）走各自独立 token 表，
            # 结构与 C1-C9 不同，不在 entity_subfolder_tokens 内。
            region_entity_maps = {"H1": "hkg_subfolder_tokens", "S1": "sgp_subfolder_tokens"}
            for ent_prefix, cfg_key in region_entity_maps.items():
                if entity.startswith(ent_prefix):
                    ent_data = config.get(cfg_key, {})
                    root_token = ent_data.get("root", "")
                    subfolder_path = "/".join(feishu_path[2:])
                    for path_key in sorted(ent_data.keys(), key=len, reverse=True):
                        if path_key != "root" and subfolder_path.startswith(path_key):
                            root_token = ent_data[path_key]
                            subfolder_path = subfolder_path[len(path_key):].lstrip("/")
                            break
                    if subfolder_path:
                        sub_parts = subfolder_path.split("/") + (remaining.split("/") if remaining else [])
                    else:
                        sub_parts = remaining.split("/") if remaining else []
                    sub_parts = [p for p in sub_parts if p]
                    sub = Path(*sub_parts) if sub_parts else Path(".")
                    return f"精确映射({entity})", root_token, sub

            if region == "工作台":
                inbox = config.get("inbox_tokens", {})
                target_name = feishu_path[-1] if feishu_path else ""
                for inbox_key, inbox_token in inbox.items():
                    if target_name and target_name in inbox_key:
                        root_token = inbox_token
                        break
                if not root_token and len(feishu_path) > 2:
                    l2 = feishu_path[2] if len(feishu_path) > 2 else ""
                    root_token = inbox.get(l2, inbox.get("财务部内部收件箱", ""))
                sub = Path(remaining) if remaining else Path(".")
                return "精确映射(收件箱)", root_token, sub

            if region == "知识库":
                kb = config.get("knowledge_base_tokens", {})
                l1 = feishu_path[1] if len(feishu_path) > 1 else ""
                root_token = kb.get(l1, "")
                sub_parts = list(feishu_path[2:]) + (remaining.split("/") if remaining else [])
                sub_parts = [p for p in sub_parts if p]
                sub = Path(*sub_parts) if sub_parts else Path(".")
                return "精确映射(知识库)", root_token, sub

            if region == "待定事项":
                root_token = config.get("root_folders", {}).get("待定事项", {}).get("folder_token", "")
                l1 = feishu_path[1] if len(feishu_path) > 1 else ""
                pending = {"待上传GDrive资料": "XbidfSbnClUau6dyI2Kc6Uxwnjc",
                           "待删除": "HiAFfqCpol26bKdE7cOcITT4npe",
                           "年度工作规划": "Hy7ffP0DClpjjVdp2ydcIEIrnUb"}
                if l1 in pending:
                    root_token = pending[l1]
                sub_parts = list(feishu_path[2:]) + (remaining.split("/") if remaining else [])
                sub_parts = [p for p in sub_parts if p]
                sub = Path(*sub_parts) if sub_parts else Path(".")
                return "精确映射(待定)", root_token, sub

    return None


def classify_file(rel_parts: tuple[str, ...], config: dict) -> tuple[str, str, Path]:
    """返回 (route_label, folder_token, sub_path)。优先精确映射，回退规则路由。"""
    mapped = classify_by_mapping(rel_parts, config)
    if mapped and mapped[1]:
        return mapped

    rel_str = "/".join(rel_parts)

    for route in config.get("routes", []):
        prefix = route["source_prefix"]
        if not rel_str.lower().startswith(prefix.lower()):
            continue

        prefix_depth = len(prefix.split("/"))
        remaining = rel_parts[prefix_depth:]
        label = route.get("label", "其他")
        rtype = route["type"]

        if rtype == "fixed":
            sub = Path(*remaining) if remaining else Path(".")
            return label, route["folder_token"], sub

        if rtype == "archive_entity":
            entity = route["entity"]
            tokens = config.get("entity_subfolder_tokens", {}).get(entity, {})
            entity_root = tokens.get("root", "")

            cat = detect_archive_category(remaining)
            if cat:
                cat_token = tokens.get(cat)
                if cat_token:
                    cat_depth = None
                    cat_prefix = cat.split(". ")[0] + "."
                    for i, part in enumerate(remaining):
                        if part.startswith(cat_prefix) or part == cat:
                            cat_depth = i
                            break
                    if cat_depth is not None:
                        after = remaining[cat_depth + 1:]
                    else:
                        after = remaining

                    if cat == "c. 会计凭证":
                        cat_token = tokens.get("c. 会计凭证")
                        if cat_token:
                            fy = extract_fiscal_year(remaining)
                            fy_key = f"FY-{fy}" if fy else None
                            fy_token = tokens.get(f"c. 会计凭证/{fy_key}") if fy_key else None
                            base_token = fy_token or cat_token
                            sub_cat = detect_voucher_subcategory(remaining)
                            sub_parts = []
                            if not fy_token and fy_key:
                                sub_parts.append(fy_key)
                            if sub_cat:
                                sub_parts.append(sub_cat)
                            sub = Path(*sub_parts) if sub_parts else Path(".")
                            return label, base_token, sub

                    if cat == "d. 其他会计资料":
                        text = "/".join(remaining)
                        if "银行对账单" in text or "bank_statement" in text.lower():
                            bs_token = tokens.get("d. 其他会计资料/银行对账单")
                            if bs_token:
                                fy = extract_fiscal_year(remaining)
                                fy_key = f"FY-{fy}" if fy else None
                                fy_token = tokens.get(f"d. 其他会计资料/银行对账单/{fy_key}") if fy_key else None
                                base_token = fy_token or bs_token
                                sub_parts = []
                                if not fy_token and fy_key:
                                    sub_parts.append(fy_key)
                                sub = Path(*sub_parts) if sub_parts else Path(".")
                                return label, base_token, sub

                    deeper_path = "/".join([cat] + list(after)) if after else cat
                    for path_key in sorted(tokens.keys(), key=len, reverse=True):
                        if deeper_path.startswith(path_key) and path_key != cat:
                            leftover = deeper_path[len(path_key):].lstrip("/")
                            sub = Path(leftover) if leftover else Path(".")
                            return label, tokens[path_key], sub

                    sub = Path(*after) if after else Path(".")
                    return label, cat_token, sub

            sub = Path(*remaining) if remaining else Path(".")
            return label, entity_root, sub

        if rtype == "direct_region":
            rest_str = "/".join(remaining)
            for rule in route.get("region_rules", []):
                if any(kw.lower() in rest_str.lower() for kw in rule["keywords"]):
                    region_idx = None
                    for i, part in enumerate(remaining):
                        if any(k.lower() in part.lower() for k in rule["keywords"]):
                            region_idx = i
                            break
                    after = remaining[region_idx + 1:] if region_idx is not None else remaining
                    sub = Path(*after) if after else Path(".")
                    return label, rule["folder_token"], sub
            continue

        if rtype == "per_entity":
            entity, eidx = resolve_chn_entity(remaining, config)
            if not entity:
                continue
            tokens = config.get("entity_subfolder_tokens", {}).get(entity, {})
            ft = tokens.get(route["target_subfolder"])
            if not ft:
                continue
            after = remaining[eidx + 1:]
            sub = Path(*after) if after else Path(".")
            return label, ft, sub

        if rtype == "per_entity_known":
            entity, eidx = resolve_chn_entity(remaining, config)
            if not entity:
                continue
            tokens = config.get("entity_subfolder_tokens", {}).get(entity, {})
            ft = tokens.get(route["target_subfolder_path"])
            if ft:
                after = remaining[eidx + 1:]
                sub = Path(*after) if after else Path(".")
                return label, ft, sub
            fallback_parent_ft = tokens.get(route.get("fallback_parent", ""))
            if fallback_parent_ft:
                create_name = route.get("fallback_create_name", "")
                after = remaining[eidx + 1:]
                sub_parts = ([create_name] if create_name else []) + list(after)
                sub = Path(*sub_parts) if sub_parts else Path(".")
                return label, fallback_parent_ft, sub
            continue

        if rtype == "invoice_split":
            entity, eidx = resolve_chn_entity(remaining, config)
            rest_str = "/".join(remaining)
            inbox_kws = route.get("inbox_keywords", [])

            if any(kw in rest_str for kw in inbox_kws):
                inbox = config.get("inbox_tokens", {})
                kw_idx = None
                ft = ""
                inbox_key = ""

                for i, part in enumerate(remaining):
                    if "对公付款" in part:
                        inbox_key = "b. 对公付款 - 付款资料提交"
                        ft = inbox.get(inbox_key, "")
                        kw_idx = i
                        break
                    elif "报销付款" in part:
                        inbox_key = "c. 报销付款 - 付款资料提交"
                        ft = inbox.get(inbox_key, "")
                        kw_idx = i
                        break
                if not ft:
                    ft = inbox.get("财务部内部收件箱", "")

                if ft and inbox_key and entity:
                    for ekw in config.get("chn_entity_keywords", {}):
                        if ekw in rest_str:
                            entity_ft = resolve_inbox_entity_token(inbox_key, ekw, config)
                            if entity_ft:
                                ft = entity_ft
                            break

                if kw_idx is not None:
                    after_kw = remaining[kw_idx + 1:]
                    sub_parts = [p for p in after_kw
                                 if not any(ekw in p for ekw in config.get("chn_entity_keywords", {}))]
                else:
                    after = remaining[eidx + 1:] if eidx is not None else remaining
                    sub_parts = list(after)
                sub = Path(*sub_parts) if sub_parts else Path(".")
                return "收件箱", ft, sub

            if entity:
                tokens = config.get("entity_subfolder_tokens", {}).get(entity, {})
                default_sub = route.get("default_subfolder", "c. 会计凭证")
                ft = tokens.get(default_sub, "")
                after = remaining[eidx + 1:]
                if ft and default_sub == "c. 会计凭证":
                    fy = extract_fiscal_year(remaining + after)
                    fy_key = f"FY-{fy}" if fy else None
                    fy_token = tokens.get(f"{default_sub}/{fy_key}") if fy_key else None
                    base_token = fy_token or ft
                    sub_cat = detect_voucher_subcategory(remaining + after)
                    sub_parts = []
                    if not fy_token and fy_key:
                        sub_parts.append(fy_key)
                    if sub_cat:
                        sub_parts.append(sub_cat)
                    sub = Path(*sub_parts) if sub_parts else Path(".")
                    return label, base_token, sub
                else:
                    sub_parts = list(after)
                    sub_parts = [p for p in sub_parts if p]
                    sub = Path(*sub_parts) if sub_parts else Path(".")
                return label, ft, sub
            continue

        if rtype == "revenue_split":
            entity, eidx = resolve_chn_entity(remaining, config)
            if not entity:
                continue
            rest_str = "/".join(remaining)
            tokens = config.get("entity_subfolder_tokens", {}).get(entity, {})
            after = remaining[eidx + 1:]
            if any(kw in rest_str for kw in route.get("split_keywords", [])):
                match_sub = route["match_subfolder"]
                sub_prefix = route.get("match_sub_prefix")
                deep_key = f"{match_sub}/{sub_prefix}" if sub_prefix else match_sub
                ft = tokens.get(deep_key) or tokens.get(match_sub, "")
                if tokens.get(deep_key):
                    fy = extract_fiscal_year(remaining + after)
                    fy_key = f"FY-{fy}" if fy else None
                    fy_token = tokens.get(f"{deep_key}/{fy_key}") if fy_key else None
                    base_token = fy_token or ft
                    sub_cat = detect_voucher_subcategory(remaining + after)
                    sub_parts = []
                    if not fy_token and fy_key:
                        sub_parts.append(fy_key)
                    if sub_cat:
                        sub_parts.append(sub_cat)
                    sub = Path(*sub_parts) if sub_parts else Path(".")
                    return label, base_token, sub
                else:
                    sub_parts = ([sub_prefix] if sub_prefix else []) + list(after)
                    sub_parts = [p for p in sub_parts if p]
                    sub = Path(*sub_parts) if sub_parts else Path(".")
            else:
                ft = tokens.get(route["default_subfolder"], "")
                sub = Path(*after) if after else Path(".")
            return label, ft, sub

    fallback = config.get("fallback_folder", {})
    sub = Path(*rel_parts) if rel_parts else Path(".")
    return "fallback", fallback.get("folder_token", ""), sub


def upload_with_retry(token: str, folder_token: str, filepath: Path, settings: dict) -> dict:
    result = None
    for _ in range(settings.get("retry_max", 3)):
        result = upload_file(token, folder_token, filepath)
        if result["status"] == "success":
            return result
        time.sleep(settings.get("retry_delay_seconds", 5))
    return result


def run_upload(dry_run: bool = False, source_prefix: str | None = None, limit: int | None = None):
    config = load_config()
    settings = config["upload_settings"]
    configure_rate_limit(settings)

    if not dry_run:
        token = get_tenant_access_token()
        if not token:
            return
    else:
        token = ""

    if not DOWNLOADS_DIR.exists():
        print(f"错误：下载目录不存在 {DOWNLOADS_DIR}")
        return

    mode_label = "dry-run 模拟" if dry_run else "上传"
    if source_prefix:
        print(f"过滤前缀：{source_prefix}")
    if limit:
        print(f"文件数上限：{limit}")
    print(f"模式：{mode_label}\n")

    date_str = datetime.now().strftime("%Y%m%d")
    suffix = "-dryrun" if dry_run else ""
    log_path = UPLOAD_LOG_DIR / f"upload-report-{date_str}{suffix}.csv"
    UPLOAD_LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_entries = []
    errors = []
    stats: dict[str, int] = {}
    processed = 0

    for root, _, filenames in os.walk(DOWNLOADS_DIR):
        for fname in filenames:
            if fname.startswith("."):
                continue

            src = Path(root) / fname
            rel = src.relative_to(DOWNLOADS_DIR)
            rel_parts = rel.parts

            if source_prefix:
                rel_str = "/".join(rel_parts)
                if not rel_str.lower().startswith(source_prefix.lower()):
                    continue

            if limit and processed >= limit:
                break

            label, folder_token, sub_path = classify_file(rel_parts, config)

            if not folder_token:
                print(f"  跳过（无目标文件夹）: {rel}")
                continue

            processed += 1

            if dry_run:
                rel_dir = sub_path.parent if sub_path != Path(".") else Path(".")
                target_desc = f"{folder_token}"
                if rel_dir != Path("."):
                    target_desc += f" / {rel_dir}"
                print(f"  [{label}] {rel}")
                print(f"    → 目标: {target_desc}")
                entry = {
                    "源路径": str(rel),
                    "路由": label,
                    "状态": "dry-run",
                    "时间": datetime.now().isoformat(),
                    "目标folder_token": folder_token,
                    "子路径": str(sub_path),
                    "错误": "",
                }
                log_entries.append(entry)
                stats[label] = stats.get(label, 0) + 1
                continue

            rel_dir = sub_path.parent if sub_path != Path(".") else Path(".")
            if rel_dir != Path("."):
                target_token = ensure_folder_path(token, folder_token, rel_dir)
            else:
                target_token = folder_token

            if not target_token:
                entry = {
                    "源路径": str(rel),
                    "路由": label,
                    "状态": "失败",
                    "文件token": "",
                    "MD5": "",
                    "时间": datetime.now().isoformat(),
                    "错误": "无法创建目标文件夹",
                }
                log_entries.append(entry)
                errors.append(str(rel))
                stats["failed"] = stats.get("failed", 0) + 1
                continue

            result = upload_with_retry(token, target_token, src, settings)

            file_md5 = md5_hash(src)
            entry = {
                "源路径": str(rel),
                "路由": label,
                "状态": result["status"],
                "文件token": result.get("file_token", ""),
                "MD5": file_md5,
                "时间": datetime.now().isoformat(),
                "错误": result.get("error", ""),
            }
            log_entries.append(entry)

            if result["status"] == "success":
                stats[label] = stats.get(label, 0) + 1
            else:
                stats["failed"] = stats.get("failed", 0) + 1
                errors.append(str(rel))

        if limit and processed >= limit:
            break

    if dry_run:
        fieldnames = ["源路径", "路由", "状态", "目标folder_token", "子路径", "时间", "错误"]
    else:
        fieldnames = ["源路径", "路由", "状态", "文件token", "MD5", "时间", "错误"]
    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(log_entries)

    if errors:
        with open(ERROR_QUEUE, "w", encoding="utf-8") as f:
            f.write("\n".join(errors))

    failed = stats.pop("failed", 0)
    total_ok = sum(stats.values())
    detail = " / ".join(f"{k} {v}" for k, v in sorted(stats.items()) if v > 0)

    print(f"\n{mode_label}完成：")
    print(f"  处理：{total_ok}（{detail}）")
    if not dry_run:
        print(f"  失败：{failed}")
    print(f"  日志：{log_path}")
    if errors:
        print(f"  失败文件清单：{ERROR_QUEUE}")


def main():
    parser = argparse.ArgumentParser(description="WPS → 飞书云盘上传工具")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行，只显示路由结果不实际上传")
    parser.add_argument("--source-prefix", type=str, default=None,
                        help="只处理指定前缀的源路径（如 '80.x 会计电子档案'）")
    parser.add_argument("--limit", type=int, default=None,
                        help="最多处理 N 个文件")
    args = parser.parse_args()
    run_upload(dry_run=args.dry_run, source_prefix=args.source_prefix, limit=args.limit)


if __name__ == "__main__":
    main()
