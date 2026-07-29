#!/usr/bin/env python3
"""在指定根文件夹下定位若干 folder token 的完整路径（只读）。

用途：用户给出飞书文件夹链接（只有 token）时，反查它在 staging 树里的
相对路径，从而确定 move-within-feishu.py 的 --source-prefix 正确坐标。

做法：从 --root（默认 staging 根）BFS 遍历子夹（drive/v1/files GET，
复用 upload-to-feishu.py 的封装：限流 + 退避 + 缓存），命中目标 token
即输出其相对路径；全部命中后提前停止遍历。

只读 GET，不写任何系统。凭证：FEISHU_APP_ID / FEISHU_APP_SECRET。

用法：
  python3 scripts/locate-token-path.py --tokens TOK1 TOK2 ... [--root ROOT_TOKEN]
  python3 scripts/locate-token-path.py --tokens TOK1 --list-children
    （--list-children 时额外列出每个命中 token 的直接子项清单）
"""

import sys
import argparse
import importlib.util
from collections import deque
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def _load_upload_module():
    path = SCRIPTS_DIR / "upload-to-feishu.py"
    spec = importlib.util.spec_from_file_location("upload_to_feishu", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


U = _load_upload_module()
DEFAULT_ROOT = "BtDzfm0djl1anldpo8ac0ulrnSg"  # 与 move-within-feishu.py 的 staging 默认一致


def main():
    ap = argparse.ArgumentParser(description="在根文件夹下反查 token 的相对路径（只读）")
    ap.add_argument("--tokens", nargs="+", required=True, help="要定位的 folder token")
    ap.add_argument("--root", default=DEFAULT_ROOT, help=f"遍历根（默认 staging {DEFAULT_ROOT}）")
    ap.add_argument("--list-children", action="store_true", help="列出命中 token 的直接子项")
    args = ap.parse_args()

    settings = U.load_config().get("upload_settings", {})
    U.configure_rate_limit(settings)
    tenant = U.get_tenant_access_token()

    targets = set(args.tokens)
    found: dict[str, str] = {}
    scanned = 0
    queue = deque([(args.root, "")])
    while queue and targets - set(found):
        ftoken, rel = queue.popleft()
        children = U.list_folder_cached(tenant, ftoken)
        scanned += 1
        for c in children:
            if c.get("type") != "folder":
                continue
            ctoken = c.get("token", "")
            crel = f"{rel}/{c.get('name', '')}" if rel else c.get("name", "")
            if ctoken in targets and ctoken not in found:
                found[ctoken] = crel
                print(f"FOUND  {ctoken}  {crel}", flush=True)
            queue.append((ctoken, crel))

    print(f"\n遍历文件夹数：{scanned}")
    missing = targets - set(found)
    for t in missing:
        print(f"MISSING  {t}  （不在根 {args.root} 之下）")

    if args.list_children:
        for t, rel in found.items():
            items = U.list_folder_cached(tenant, t)
            nfolder = sum(1 for i in items if i.get("type") == "folder")
            nfile = len(items) - nfolder
            print(f"\n=== {rel} [{t}]  子夹{nfolder} 文件{nfile} ===")
            for i in items:
                print(f"  [{i.get('type')}] {i.get('name')}  {i.get('token')}")

    sys.exit(0 if not missing else 2)


if __name__ == "__main__":
    main()
