#!/usr/bin/env python3
"""断点续跑离线单测：不触碰飞书，只验证 checkpoint 读写/恢复语义。"""
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path("/home/fiona/2_FIN_CHN/wps-to-feishu-migration/scripts")
spec = importlib.util.spec_from_file_location("mv", SCRIPTS / "move-within-feishu.py")
mv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mv)

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# 1. 同批（staging+prefix）路径稳定，不同批互不相扰
p1 = mv.checkpoint_path("STAGE1", "2.0 收入管理")
p2 = mv.checkpoint_path("STAGE1", "2.0 收入管理")
p3 = mv.checkpoint_path("STAGE1", "3.0 其他")
p4 = mv.checkpoint_path("STAGE2", "2.0 收入管理")
check("同批路径稳定", p1 == p2)
check("不同前缀不同文件", p1 != p3)
check("不同staging不同文件", p1 != p4)
check("前缀大小写不敏感", mv.checkpoint_path("S", "ABC") == mv.checkpoint_path("S", "abc"))

# 2. load_checkpoint：done 状态载入、失败行丢弃（续跑重试）、meta 行忽略、半行容错
tmp = Path("/tmp/test-ckpt.jsonl")
rows = [
    {"_meta": 1, "staging": "S", "prefix": "x"},
    {"源路径": "a/f1.pdf", "状态": "success", "文件token": "T1"},
    {"源路径": "a/f2.pdf", "状态": "跳过-同名", "文件token": "T2"},
    {"源路径": "a/f3.pdf", "状态": "失败", "文件token": "T3", "错误": "err"},
    {"源路径": "a/f4.pdf", "状态": "跳过-fallback保留原位", "文件token": "T4"},
    {"源路径": "a/f5.pdf", "状态": "跳过-无目标", "文件token": "T5"},
]
with open(tmp, "w", encoding="utf-8") as fp:
    for r in rows:
        fp.write(json.dumps(r, ensure_ascii=False) + "\n")
    fp.write('{"源路径": "a/f6.pdf", "状态": "succ')  # 模拟被杀瞬间的半行

entries = mv.load_checkpoint(tmp)
tokens = {e["文件token"] for e in entries}
check("done状态全部载入", tokens == {"T1", "T2", "T4", "T5"})
check("失败行不算已处理（续跑会重试）", "T3" not in tokens)
check("半行不炸且被忽略", all(e.get("文件token") != "" for e in entries))

# 3. 不存在的断点文件 → 空列表（首跑）
check("无断点文件返回空", mv.load_checkpoint(Path("/tmp/no-such-ckpt.jsonl")) == [])

tmp.unlink(missing_ok=True)
print()
if fails:
    print(f"共 {len(fails)} 项失败"); sys.exit(1)
print("全部通过")
