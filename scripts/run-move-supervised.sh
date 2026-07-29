#!/usr/bin/env bash
# 迁移监督器：拉起 move-within-feishu.py，进程被杀/崩溃时自动重启断点续跑，
# 直到正常跑完（exit 0）或达到最大重试次数。
#
# 用法（参数原样透传给 move-within-feishu.py）：
#   setsid nohup bash scripts/run-move-supervised.sh \
#       --source-prefix "2.0 收入管理" >/dev/null 2>&1 & disown
#
# 环境变量：
#   SUPERVISOR_LOG   日志文件名（默认 run-supervised.log，落在 04-upload-logs/）
#   MAX_ATTEMPTS     最大尝试次数（默认 20）
#   RETRY_DELAY      两次尝试间隔秒数（默认 15）
set -u
cd "$(dirname "$0")/.."

# 加载飞书凭证等环境变量（detached 启动时不继承交互 shell 的环境）
if [ -f 00-config/.env ]; then
  set -a; . 00-config/.env; set +a
fi

LOG="04-upload-logs/${SUPERVISOR_LOG:-run-supervised.log}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-20}"
RETRY_DELAY="${RETRY_DELAY:-15}"

echo "[supervisor] 启动 pid=$$ args=$* $(date -Is)" >> "$LOG"

attempt=0
while (( attempt < MAX_ATTEMPTS )); do
  attempt=$((attempt + 1))
  echo "[supervisor] 第 $attempt/$MAX_ATTEMPTS 次尝试 $(date -Is)" >> "$LOG"
  python3 scripts/move-within-feishu.py "$@" >> "$LOG" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "[supervisor] 本批正常跑完（rc=0）$(date -Is)" >> "$LOG"
    exit 0
  fi
  echo "[supervisor] 进程中断（rc=$rc），${RETRY_DELAY}s 后断点续跑 $(date -Is)" >> "$LOG"
  sleep "$RETRY_DELAY"
done

echo "[supervisor] 连续 $MAX_ATTEMPTS 次未跑完，停止重试，请人工介入 $(date -Is)" >> "$LOG"
exit 1
