#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feishu-download.py — 下载飞书云盘文件到本地（可复用助手）

迁移工程全程服务端 move/copy，从不需要把文件内容拉到本地；本脚本补上"下载字节"能力。
两条路径：
  1) batch_get_tmp_download_urls（签名 CDN URL，推荐，可跨网下载）
  2) medias/{token}/download（bearer 直下，兜底）

用法：
  python3 feishu-download.py <file_token> <out_path>
  python3 feishu-download.py <file_token> <out_path> --type folder   # folder_token 下所有文件

授权：FINHKG-154 dev-ticket grant（wps-to-feishu-migration/scripts/**）。
"""
import argparse, json, os, sys, time
from pathlib import Path
import importlib.util
import requests

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("U", HERE / "upload-to-feishu.py")
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)


def tmp_download(token, file_token):
    """签名 CDN URL 下载。返回 (bytes|None, diag)。"""
    url = f"{U.FEISHU_BASE}/drive/v1/medias/batch_get_tmp_download_url"
    # raw requests 直打，排除 feishu_request 封装干扰
    import requests as _rq
    r = _rq.post(url, headers={**U.get_headers(token), "Content-Type": "application/json"},
                 json={"file_tokens": [file_token]}, timeout=30)
    print(f"[debug] POST {url}\n[debug] status={r.status_code} body={r.text[:300]}", file=sys.stderr)
    diag = f"tmp_urls status={r.status_code}"
    try:
        body = r.json()
        diag += f" code={body.get('code')} msg={body.get('msg','')}"
        if body.get("code") != 0:
            return None, diag
        urls = (body.get("data") or {}).get("tmp_download_urls", [])
        if not urls:
            return None, diag + " no_urls"
        durl = urls[0].get("tmp_download_url")
        if not durl:
            return None, diag + " empty_url"
        rr = requests.get(durl, timeout=120, allow_redirects=True)
        diag += f" | cdn status={rr.status_code} bytes={len(rr.content)}"
        if rr.status_code == 200 and rr.content:
            return rr.content, diag
        return None, diag
    except Exception as e:
        return None, diag + f" exc={e} body={r.text[:200]}"


def media_download(token, file_token):
    """bearer 直下兜底。"""
    url = f"{U.FEISHU_BASE}/drive/v1/medias/{file_token}/download"
    r = U.feishu_request("GET", url, headers=U.get_headers(token), allow_redirects=True)
    diag = f"medias status={r.status_code} bytes={len(r.content)} ct={r.headers.get('Content-Type')}"
    if r.status_code == 200 and r.content:
        return r.content, diag
    return None, diag


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file_token")
    ap.add_argument("out_path")
    ap.add_argument("--type", default="file", help="file | folder")
    args = ap.parse_args()

    token = U.get_tenant_access_token()
    if not token:
        sys.exit("拿不到 tenant token")

    if args.type == "folder":
        # 列出文件夹下所有 file，逐个下载
        items = [it for it in U.list_folder_live(token, args.file_token) if it.get("type") == "file"]
        print(f"文件夹 {args.file_token[:8]} 下 {len(items)} 个文件")
        Path(args.out_path).mkdir(parents=True, exist_ok=True)
        ok = 0
        for it in items:
            data, diag = tmp_download(token, it["token"])
            if data is None:
                data, diag2 = media_download(token, it["token"])
                diag = diag + " | " + diag2
            if data:
                safe = "".join(c for c in it["name"] if c not in '/\\\0')
                (Path(args.out_path) / safe).write_bytes(data)
                ok += 1
                print(f"  ✓ {it['name']} ({len(data):,}B)")
            else:
                print(f"  ✗ {it['name']}  {diag}")
        print(f"完成 {ok}/{len(items)}")
        return

    data, diag = tmp_download(token, args.file_token)
    method = "tmp_url"
    if data is None:
        data, diag2 = media_download(token, args.file_token)
        diag = diag + " || " + diag2
        method = "medias"
    print(f"[{method}] {diag}")
    if data is None:
        sys.exit("下载失败（两条路径均被拒）")
    Path(args.out_path).write_bytes(data)
    print(f"✓ 写入 {args.out_path}（{len(data):,} bytes）")


if __name__ == "__main__":
    main()
