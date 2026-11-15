#!/usr/bin/env bash
# SnowLuma 在 VM 里的启动包装：把「无人值守」需要的环境变量一次写死。
#
# 三个事实决定了这几行：
#   1. EULA/PRIVACY 是版本化闸门，环境变量只在本进程内生效（不写 consent.json），
#      重装/升级后依然有效，不会因为版本号变了又卡住。
#   2. hookAutoLoad 写在 config/runtime.json 里（true）—— 每次发现 QQ 进程就注入。
#   3. OneBot 的监听地址在 config/onebot_<uin>.json，这里不改；由部署脚本写成 0.0.0.0，
#      否则 VBox NAT 的端口转发到不了 127.0.0.1 上的服务。
set -euo pipefail
cd "$(dirname "$0")"

export SNOWLUMA_ACCEPT_EULA=1
export SNOWLUMA_ACCEPT_PRIVACY=1
export SNOWLUMA_LOG_LEVEL="${SNOWLUMA_LOG_LEVEL:-info}"
export SNOWLUMA_LOG_FILE_LEVEL="${SNOWLUMA_LOG_FILE_LEVEL:-info}"

exec ./launcher.sh
