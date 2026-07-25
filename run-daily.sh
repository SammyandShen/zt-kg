#!/bin/bash
# run-daily.sh — 每日收盘后：抓当日涨停池 → 重建网页数据（launchd 入口）
set -e
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$PROJECT_DIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') run-daily start ====="
python3 scripts/fetch_daily.py
# 新闻关联失败不阻断网页重建（东财接口偶发抖动，次日会自动补缺）
python3 scripts/fetch_news.py --days 2 || echo "⚠️ fetch_news 失败，跳过"
# 巨潮官方公告标题只作为事件上下文；题材未被明确点名时绝不自动绑定或核实。
python3 scripts/fetch_event_announcements.py --days 2 || echo "⚠️ fetch_event_announcements 失败，跳过"
# LLM一句话归因（claude CLI 走订阅；幂等，--days 2 顺带补昨日）
python3 scripts/summarize_news.py --days 2 || echo "⚠️ summarize_news 失败，跳过"
# 年报业务链（零人工干预）：抓年报→抽业务候选→(rebuild内自动晋升 conf≥0.7 主营)
python3 scripts/fetch_company_reports.py --days 2 || echo "⚠️ fetch_company_reports 失败，跳过"
python3 scripts/extract_business_candidates.py || echo "⚠️ extract_business_candidates 失败，跳过"
# 新出现的长尾标签先登记为 candidate，再过质量门禁；不合格时停止导出/发布。
python3 scripts/gen_tag_meta.py --all
# 标签自动分型链（零人工干预）：sonnet增量复核(台账跳过已判)→高置信转正+父建议四道闸挂树
python3 scripts/classify_tags.py || echo "⚠️ classify_tags 失败，跳过"
python3 scripts/auto_adopt_tags.py || echo "⚠️ auto_adopt_tags 失败，跳过"
# 原因标签只生成“候选题材关系”；人工业务事实/归因来自版本化 JSON，
# 自动轮次保持 provisional，避免把供应商原因直接写成正式主营或题材。
python3 scripts/rebuild_semantic_layer.py
# 候选题材按后续交易日推进T0/T+1/T+2复核；只给旁证建议，不自动升级verified。
python3 scripts/review_theme_attributions.py --days 5
# LLM判决三队列（用户授权代行人工判决；保守规则=拿不准一律leave；失败不阻断）
python3 scripts/judge_attributions.py || echo "⚠️ judge_attributions 失败，跳过"
# 重放当日新判决（facts_overrides）到语义层
python3 scripts/rebuild_semantic_layer.py
python3 scripts/audit_tags.py
python3 scripts/build_site.py
# 公网发布（存在 .deploy-enabled 开关文件才执行；见 deploy.sh 头部说明）
if [ -f "$PROJECT_DIR/.deploy-enabled" ]; then
    bash "$PROJECT_DIR/deploy.sh" || echo "⚠️ 部署失败，本地数据不受影响"
fi
echo "===== done ====="
