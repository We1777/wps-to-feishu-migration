#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
upload-one-file.py — 把单个文件上传到飞书云盘指定文件夹（复用 upload-to-feishu.py 的 upload_file）。

用法：
  python3 upload-one-file.py --folder <folder_token> <filepath>
成功时 stdout 出文件链接（https://<domain>/file/<token>）。

授权：FINHKG-154 dev-ticket grant（wps-to-feishu-migration/scripts/**）。
"""
import argparse, sys
from pathlib import Path
import importlib.util

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("U", HERE / "upload-to-feishu.py")
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

DOMAIN = "lcnzfxq3rlhh.feishu.cn"


def main():
    ap = argparse.ArgumentParser(description="上传单个文件到飞书指定文件夹")
    ap.add_argument("--folder", required=True, help="目标文件夹 token")
    ap.add_argument("filepath", help="要上传的文件路径")
    a = ap.parse_args()

    fp = Path(a.filepath)
    if not fp.exists():
        sys.exit(f"文件不存在：{fp}")
    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token，检查 FEISHU_APP_ID/FEISHU_APP_SECRET")
    r = U.upload_file(token, a.folder, fp)
    if r.get("status") == "success":
        print(f"https://{DOMAIN}/file/{r['file_token']}")
    else:
        sys.exit(f"上传失败：{r}")


if __name__ == "__main__":
    main()
