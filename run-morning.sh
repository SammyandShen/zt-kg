#!/bin/bash
# run-morning.sh — 工作日早盘前（07:40）补齐昨日涨停信息（launchd 入口）。
#
# 为什么需要第二班次：17:00 主班次为了不拖慢当日发布，把年报提取/互动抓取放在
# 发布线之后，产生的业务候选要等第二天 17:00 的 rebuild 才晋升；公告和互动回复
# 也多在晚间才发出。本班次在开盘前把这些 T+1 信息收拢：候选晋升成业务事实、
# 隔夜证据喂给 judge、归因尽早转正——9 点前页面上昨日涨停股就是完整的。
#
# 不做的事：不抓当日涨停池（盘前无数据）、不跑分型/映射/挂树等慢工序（17:00 管）。
set -e
PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$PROJECT_DIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') run-morning start ====="
# --days 4：周一早晨要覆盖到上周五的事件
python3 scripts/fetch_news.py --days 4 || echo "⚠️ fetch_news 失败，跳过"
python3 scripts/fetch_event_announcements.py --days 4 || echo "⚠️ fetch_event_announcements 失败，跳过"
python3 scripts/summarize_news.py --days 4 || echo "⚠️ summarize_news 失败，跳过"
# summarize 可能引入新标签；不登记会撞 audit 门禁
python3 scripts/gen_tag_meta.py --all
# 年报业务链：昨日新面孔的年报若 17:00 班次没抓完/没提取完，此处收尾
python3 scripts/fetch_company_reports.py --days 4 || echo "⚠️ fetch_company_reports 失败，跳过"
python3 scripts/extract_business_candidates.py --limit 60 || echo "⚠️ extract_business_candidates 存在部分失败项"
# 互动平台：公司多在晚间回复，早晨抓一轮正好收割
python3 scripts/fetch_interactive_qa.py --days 4 --limit 40 || echo "⚠️ fetch_interactive_qa 存在失败项"
# rebuild：年报候选自动晋升 verified、互动候选入事实域、link_basis 重算
python3 scripts/rebuild_semantic_layer.py
python3 scripts/review_theme_attributions.py --days 5
# 隔夜证据在手，judge 尽早判决，昨日归因不用再等到 17:00
python3 scripts/judge_attributions.py || echo "⚠️ judge_attributions 失败，跳过"
python3 scripts/rebuild_semantic_layer.py
python3 scripts/audit_tags.py
python3 scripts/build_site.py
if [ -f "$PROJECT_DIR/.deploy-enabled" ]; then
    bash "$PROJECT_DIR/deploy.sh" || echo "⚠️ 部署失败，本地数据不受影响"
fi
echo "===== done ====="
