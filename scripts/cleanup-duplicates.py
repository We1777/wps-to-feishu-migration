#!/usr/bin/env python3
"""飞书云盘重复文件夹盘点 / 合并清理工具。

背景：move-within-feishu.py 早期 create_folder 采用「建完 pop 父夹缓存 → 下一个文件
再 list 父夹」的去重方式，撞上飞书云盘 read-after-write 延迟——刚建的夹还没进列表，
于是同名夹被重复创建（月份层、凭证号层都中招），文件被劈进两套并行夹。

本工具三种模式（均可加 --dry-run 预演）：
  --inventory <token> <name>   只读盘点：递归列出子树，报告重复同名子夹、各夹文件数
  --validate-tokens            只读校验：target-space.json 里每个 token 是否在飞书真实存在
  --merge <token> <name>       合并清理：把重复同名子夹归并成 1 个，文件搬到保留夹，
                               空的重复夹删除（危险操作，务必先 --dry-run 看清单）

只用飞书 GET/move/delete，不上传、不改本地。带 4 QPS 限流 + 429 退避（复用 U.feishu_request）。
"""
import sys
import csv
import json
import argparse
import importlib.util
from pathlib import Path
from datetime import datetime
from collections import defaultdict

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
CONFIG_PATH = PROJECT_ROOT / "00-config" / "target-space.json"
LOGS = PROJECT_ROOT / "04-upload-logs"

spec = importlib.util.spec_from_file_location("upload_to_feishu", SCRIPTS / "upload-to-feishu.py")
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)


def get_token():
    tk = U.get_tenant_access_token()
    if not tk:
        print("无法获取飞书 token")
        sys.exit(1)
    return tk


def list_children_live(token, ftoken):
    """直接 GET 飞书真实子项（不走 U 的 process 缓存，确保读真实状态）。"""
    url = f"{U.FEISHU_BASE}/drive/v1/files"
    out, page = [], ""
    while True:
        params = {"folder_token": ftoken, "page_size": 200}
        if page:
            params["page_token"] = page
        r = U.feishu_request("GET", url, headers=U.get_headers(token), params=params)
        d = r.json()
        if d.get("code") != 0:
            return None  # 该 token 不存在 / 无权限
        out.extend(d.get("data", {}).get("files", []))
        page = d.get("data", {}).get("next_page_token", "")
        if not page:
            break
    return out


def cmd_inventory(token, root_token, root_name):
    dup_reports, file_counts = [], []
    totals = {"folders": 0, "files": 0}

    def walk(ftoken, path):
        children = list_children_live(token, ftoken)
        if children is None:
            print(f"  !! 无法读取（token 不存在?）: {path} [{ftoken}]")
            return
        folders = [c for c in children if c.get("type") == "folder"]
        files = [c for c in children if c.get("type") != "folder"]
        totals["files"] += len(files)
        if files:
            file_counts.append((path, len(files)))
        by_name = defaultdict(list)
        for f in folders:
            by_name[f.get("name", "")].append(f.get("token", ""))
        for name, toks in by_name.items():
            totals["folders"] += len(toks)
            if len(toks) > 1:
                dup_reports.append((path, name, len(toks), toks))
        for f in folders:
            walk(f.get("token", ""), f"{path}/{f.get('name', '')}")

    print(f"=== 盘点 {root_name} ({root_token}) ===")
    walk(root_token, root_name)
    print(f"\n子夹总数(含重复): {totals['folders']}   文件总数: {totals['files']}")
    print(f"\n=== 重复同名子夹 ({len(dup_reports)} 组) ===")
    for path, name, cnt, toks in sorted(dup_reports):
        print(f"  {path}/{name}  ×{cnt}   {toks}")
    if not dup_reports:
        print("  （无重复）")
    print(f"\n=== 含文件的夹 ({len(file_counts)}) ===")
    for path, n in sorted(file_counts):
        print(f"  {n:4d}  {path}")


def collect_tokens(config):
    """从 target-space.json 收集所有 (来源路径, token)。"""
    pairs = []

    def add(where, tk):
        if tk and isinstance(tk, str) and len(tk) > 10:
            pairs.append((where, tk))

    for name, data in config.get("root_folders", {}).items():
        add(f"root_folders/{name}", data.get("folder_token", ""))
    for entity, toks in config.get("entity_subfolder_tokens", {}).items():
        for k, tk in toks.items():
            add(f"entity/{entity}/{k}", tk)
    for cfg_key in ("hkg_subfolder_tokens", "sgp_subfolder_tokens", "chn_summary_tokens",
                    "inbox_tokens", "knowledge_base_tokens", "pending_tokens"):
        for k, tk in config.get(cfg_key, {}).items():
            add(f"{cfg_key}/{k}", tk)
    for inbox_key, ent_map in config.get("inbox_entity_tokens", {}).items():
        for ent, tk in ent_map.items():
            add(f"inbox_entity/{inbox_key}/{ent}", tk)
    return pairs


def cmd_validate_tokens(token):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    pairs = collect_tokens(config)
    print(f"=== 校验 target-space.json 中 {len(pairs)} 个 token 是否在飞书存在 ===\n")
    dead, seen = [], {}
    for where, tk in pairs:
        if tk in seen:
            ok = seen[tk]
        else:
            ok = list_children_live(token, tk) is not None
            seen[tk] = ok
        if not ok:
            dead.append((where, tk))
    if dead:
        print(f"!! 失效/不存在 token（{len(dead)} 个）：")
        for where, tk in dead:
            print(f"  {where}  →  {tk}")
    else:
        print("全部 token 在飞书真实存在 ✓")
    print(f"\n合计 {len(pairs)} 条、去重 {len(seen)} 个唯一 token，失效 {len(dead)}。")


def move_item(token, item_token, item_type, dest_folder_token):
    """把文件 / 文件夹移动到目标夹。item_type: 'file' | 'folder'。"""
    url = f"{U.FEISHU_BASE}/drive/v1/files/{item_token}/move"
    r = U.feishu_request("POST", url, headers=U.get_headers(token), json={
        "type": item_type, "folder_token": dest_folder_token})
    d = r.json()
    return d.get("code") == 0, d.get("msg", r.text)


def delete_folder(token, ftoken):
    """删除（移入回收站，可恢复）一个空文件夹。"""
    url = f"{U.FEISHU_BASE}/drive/v1/files/{ftoken}"
    r = U.feishu_request("DELETE", url, headers=U.get_headers(token), params={"type": "folder"})
    d = r.json()
    return d.get("code") == 0, d.get("msg", r.text)


def cmd_merge(token, root_token, root_name, dry_run):
    """合并去重：把每一层的重复同名子夹归并成 1 个「保留夹」（同名组第一个），
    其余重复夹的内容递归并入保留夹，最后删除已空的重复夹。

    安全护栏（用户强调「这次好好写明白」，绝不误删）：
      · 删除前必做一次实时复查，只有确认「读得到且为空」才删；
      · list 失败一律按「非空」处理 → 绝不在不确定状态下删；
      · 同名文件冲突（保留夹已有同名）→ 文件留原位、重复夹不删，写进报告让人工裁决；
      · move / delete 失败 → 保守按非空处理，不再删父夹。
    """
    plan = defaultdict(int)
    log = []  # (动作, 路径, token, 备注)

    def rec_log(action, path, tok, note=""):
        log.append({"动作": action, "路径": path, "token": tok, "备注": note})

    def safe_delete_if_empty(ftoken, path):
        """删除前实时复查：读得到且为空才删；否则一律不删。返回是否已删。"""
        ch = list_children_live(token, ftoken)
        if ch is None:
            rec_log("删除前复查失败-跳过不删", path, ftoken, "list 返回 None")
            return False
        if len(ch) != 0:
            rec_log(f"非空({len(ch)})-保留不删", path, ftoken)
            return False
        if dry_run:
            plan["delete_folders"] += 1
            rec_log("[dry-run]将删除空重复夹", path, ftoken)
            return True
        ok, msg = delete_folder(token, ftoken)
        if ok:
            plan["delete_folders"] += 1
            rec_log("删除空重复夹", path, ftoken)
            return True
        rec_log("删除失败-保留", path, ftoken, msg)
        return False

    def merge_into(src_token, dst_token, path):
        """把 src 夹内容并入 dst 夹（递归）。返回 src 是否可视为已空（可删）。"""
        src_children = list_children_live(token, src_token)
        if src_children is None:
            rec_log("读取重复夹失败-跳过不删", path, src_token, "list 返回 None")
            return False
        dst_children = list_children_live(token, dst_token)
        if dst_children is None:
            rec_log("读取保留夹失败-跳过", path, dst_token, "list 返回 None")
            return False
        dst_files = {c.get("name", "") for c in dst_children if c.get("type") != "folder"}
        dst_folders = {c.get("name", ""): c.get("token", "")
                       for c in dst_children if c.get("type") == "folder"}
        empty = True
        for c in src_children:
            nm, tp, tk = c.get("name", ""), c.get("type", ""), c.get("token", "")
            cpath = f"{path}/{nm}"
            if tp == "folder":
                if nm in dst_folders:
                    child_empty = merge_into(tk, dst_folders[nm], cpath)
                    if not (child_empty and safe_delete_if_empty(tk, cpath)):
                        empty = False
                else:
                    if dry_run:
                        plan["move_folders"] += 1
                        rec_log("[dry-run]整夹移入保留夹", cpath, tk)
                    else:
                        ok, msg = move_item(token, tk, "folder", dst_token)
                        if ok:
                            plan["move_folders"] += 1
                            rec_log("整夹移入保留夹", cpath, tk)
                        else:
                            empty = False
                            rec_log("移动子夹失败-保留", cpath, tk, msg)
            else:
                if nm in dst_files:
                    plan["collisions"] += 1
                    empty = False
                    rec_log("同名文件冲突-留原位待人工", cpath, tk)
                elif dry_run:
                    dst_files.add(nm)
                    plan["move_files"] += 1
                    rec_log("[dry-run]移动文件入保留夹", cpath, tk)
                else:
                    ok, msg = move_item(token, tk, "file", dst_token)
                    if ok:
                        dst_files.add(nm)
                        plan["move_files"] += 1
                        rec_log("移动文件入保留夹", cpath, tk)
                    else:
                        empty = False
                        rec_log("移动文件失败-保留", cpath, tk, msg)
        return empty

    def consolidate(parent_token, parent_path):
        children = list_children_live(token, parent_token)
        if children is None:
            rec_log("读取失败-跳过分支", parent_path, parent_token, "list 返回 None")
            return
        by_name = defaultdict(list)
        for f in children:
            if f.get("type") == "folder":
                by_name[f.get("name", "")].append(f.get("token", ""))
        for name, toks in by_name.items():
            if len(toks) > 1:
                plan["dup_groups"] += 1
                keeper, dups = toks[0], toks[1:]
                cpath = f"{parent_path}/{name}"
                rec_log(f"重复组×{len(toks)} 保留首个", cpath, keeper,
                        f"重复 {len(dups)} 个: {dups}")
                for dup in dups:
                    if merge_into(dup, keeper, cpath):
                        safe_delete_if_empty(dup, cpath)
        # 递归进每个（已合并的）保留夹
        for name, toks in by_name.items():
            consolidate(toks[0], f"{parent_path}/{name}")

    mode = "合并去重 dry-run 预演（不移动/不删除）" if dry_run else "合并去重执行"
    print(f"=== {mode}: {root_name} ({root_token}) ===")
    consolidate(root_token, root_name)

    stamp = datetime.now().strftime("%Y%m%d")
    suffix = "-dryrun" if dry_run else ""
    out = LOGS / f"merge-report-{stamp}{suffix}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=["动作", "路径", "token", "备注"])
        w.writeheader()
        w.writerows(log)
    print(f"\n重复组: {plan['dup_groups']}   计划/实际移动文件: {plan['move_files']}"
          f"   整夹移动: {plan['move_folders']}   删除空重复夹: {plan['delete_folders']}"
          f"   同名文件冲突(留原位): {plan['collisions']}")
    print(f"报告: {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", nargs=2, metavar=("TOKEN", "NAME"))
    ap.add_argument("--validate-tokens", action="store_true")
    ap.add_argument("--merge", nargs=2, metavar=("TOKEN", "NAME"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = U.load_config().get("upload_settings", {})
    U.configure_rate_limit(settings)
    token = get_token()

    if args.inventory:
        cmd_inventory(token, args.inventory[0], args.inventory[1])
    elif args.validate_tokens:
        cmd_validate_tokens(token)
    elif args.merge:
        # live 合并会移动/删除/建夹，须与其它写进程（移动/合并）互斥，防并发重复夹。
        # dry-run 只读，免锁。
        lock_fp = None
        if not args.dry_run:
            try:
                lock_fp = U.acquire_write_lock(label="cleanup-duplicates-merge")
            except U.WriteLockHeld as e:
                print(f"已有飞书写进程在跑（{e}），拒绝并发启动以防重复夹。"
                      f"\n请等它结束或确认无残留进程后再跑。")
                return
        try:
            cmd_merge(token, args.merge[0], args.merge[1], args.dry_run)
        finally:
            U.release_write_lock(lock_fp)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
