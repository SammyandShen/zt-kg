#!/bin/bash
# deploy.sh — 发布到 GitHub Pages（仓库 SammyandShen/zt-kg，main 分支 /docs 目录）
# 站点地址: https://sammyandshen.github.io/zt-kg/
# run-daily.sh 在 .deploy-enabled 存在时每日自动调用；手动发布直接 bash deploy.sh
set -e
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$PROJECT_DIR"

git add -A
if git diff --cached --quiet; then
    echo "ℹ️ 无变更，跳过发布"
    exit 0
fi
git commit -q -m "data: $(date '+%Y-%m-%d %H:%M') 数据更新"
git push -q origin main
echo "✅ 已推送，1-2分钟后生效: https://sammyandshen.github.io/zt-kg/"
