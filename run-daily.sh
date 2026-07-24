#!/bin/bash
# run-daily.sh — 每日收盘后：抓当日涨停池 → 重建网页数据（launchd 入口）
set -e
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$PROJECT_DIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') run-daily start ====="
python3 scripts/fetch_daily.py
# 新闻关联失败不阻断网页重建（东财接口偶发抖动，次日会自动补缺）
python3 scripts/fetch_news.py --days 2 || echo "⚠️ fetch_news 失败，跳过"
# LLM一句话归因（claude CLI 走订阅；幂等，--days 2 顺带补昨日）
python3 scripts/summarize_news.py --days 2 || echo "⚠️ summarize_news 失败，跳过"
# 新出现的长尾标签先登记为 candidate，再过质量门禁；不合格时停止导出/发布。
python3 scripts/gen_tag_meta.py --all
# 原因标签只生成“候选题材关系”；人工业务事实/归因来自版本化 JSON，
# 自动轮次保持 provisional，避免把供应商原因直接写成正式主营或题材。
python3 scripts/rebuild_semantic_layer.py
python3 scripts/audit_tags.py
python3 scripts/build_site.py
# 公网发布（存在 .deploy-enabled 开关文件才执行；见 deploy.sh 头部说明）
if [ -f "$PROJECT_DIR/.deploy-enabled" ]; then
    bash "$PROJECT_DIR/deploy.sh" || echo "⚠️ 部署失败，本地数据不受影响"
fi
echo "===== done ====="
