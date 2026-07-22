#!/bin/bash
# deploy.sh — 把 docs/ 发布到 Cloudflare Pages
#
# 一次性准备（只需做一遍）：
#   1. 注册 Cloudflare 账号（免费）: https://dash.cloudflare.com/sign-up
#   2. npm install -g wrangler
#   3. wrangler login                       # 浏览器里点授权
#   4. wrangler pages project create zt-kg --production-branch main
#   5. touch .deploy-enabled                # 打开每日自动部署开关
# 之后每天 17:00 run-daily.sh 会自动推送最新数据。
# 手动发布：bash deploy.sh
set -e
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_NAME="${ZTKG_PAGES_PROJECT:-zt-kg}"

command -v wrangler >/dev/null 2>&1 || {
    echo "❌ 未安装 wrangler，先执行: npm install -g wrangler && wrangler login"; exit 1; }

wrangler pages deploy "$PROJECT_DIR/docs" --project-name "$PROJECT_NAME" --commit-dirty=true
echo "✅ 已发布：https://$PROJECT_NAME.pages.dev"
