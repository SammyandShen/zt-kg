# zt-kg — 涨停概念知识图谱

A股每日涨停股 + 涨停原因（概念题材）长周期追踪库。核心用途：拿到最新涨停股票，
快速查它属于什么概念、板块内有哪些股票、板块近期热度，辅助龙头跟涨等短线决策。

**所有输出为行情数据整理，不是投资建议。**

## 数据真源与职责边界

| 文件 | 谁写 | 说明 |
|---|---|---|
| `data/ztkg.db` | 脚本（fetch_daily/backfill）| SQLite 主库，唯一数据真源 |
| `data/aliases.json` | 人工/LLM（用户确认后）| 概念别名映射，改后必须跑 `rebuild_tags.py` |
| `data/taxonomy.json` | 人工/LLM（用户确认后）| 板块标签层级库（父→子，支持多级/多父），改后跑 `build_site.py` 即生效 |
| `docs/data.js` | 脚本 `build_site.py` | **禁止手改** |
| `docs/index.html` | 人工维护 | 单页应用（file:// 直接打开即可用） |

- `limit_up_events.reason_type` 永存原始涨停原因；`event_concepts` 是派生表，
  可随时由 `rebuild_tags.py` 全量重建，概念合并规则可无限迭代。
- 数据源：同花顺涨停池 API（**约1年滚动窗口**，更早日期报"date参数不合法"）。
  二期扩3年需接问财/开盘啦（schema 已留 source 字段，按日选源避免双计）。
- `lb_count`（连板数）由 high_days_value 解码：`value >> 16` = 板数。
- 新闻关联：东财搜索接口按股票名逐只查，取 [涨停日-2, +1] 窗口内新闻，
  标题点名的优先、榜单类降权，每股每日最多3条。news_log 保证幂等。
  盘中抓的当天新闻质量一般（个股解读稿晚间才发），次日 --days 2 自动补优。
  data.js 只导出最近60个交易日的新闻（控体积）。
- LLM一句话归因（summarize_news.py）：调本机 Codex CLI 无头模式（走订阅无API费），
  haiku 模型，每交易日一次调用打包全部涨停股，输出 JSON 存 briefs 表。
  提示词要求自然语句禁止"+"串标签。Codex 不在 launchd PATH 时脚本自动找
  ~/.local/bin 等位置。改提示词后用 --force 重写。

## 常用命令

```bash
python3 scripts/query.py codes 300750,600519   # 批量归类（核心场景）
python3 scripts/query.py stock 300750          # 个股涨停史+概念
python3 scripts/query.py concept 算力租赁       # 概念成分股+活跃度
python3 scripts/query.py date 2026-07-21       # 某日复盘
python3 scripts/query.py similar               # 疑似应合并概念对
python3 scripts/query.py tree                  # 标签层级树+板块热度
python3 scripts/backfill.py                    # 回补历史（断点续传，只补缺）
python3 scripts/fetch_news.py --days 5         # 涨停关联新闻（东财搜索，幂等只补缺）
python3 scripts/rebuild_tags.py                # aliases.json 改后重建派生表
python3 scripts/build_site.py                  # 重导出网页数据
bash install-launchd.sh status                 # 定时任务状态（工作日17:00）
bash deploy.sh                                 # 手动发布到 Cloudflare Pages
open docs/index.html                           # 打开交互网页
```

## 对话内查询约定

直接 `sqlite3 data/ztkg.db` 或 query.py 均可。常用 join：
`limit_up_events e ⋈ event_concepts ec ⋈ concepts c`；日期格式 `YYYY-MM-DD`。

## 公网部署

- **线上地址：https://sammyandshen.github.io/zt-kg/**（GitHub Pages，
  仓库 SammyandShen/zt-kg 公开，main 分支 /docs 目录，含 .nojekyll）
- deploy.sh = git add/commit/push；`.deploy-enabled` 存在时 run-daily 每日
  17:00 自动发布（该开关文件在 .gitignore 里，删掉即停）
- db/logs 不入库（.gitignore），公网只暴露 docs/ 静态内容
- 移动端已适配（≤640px 压缩头部、双列启动卡、热力图横向滚动+固定首列）
- 注意：github.io 大陆访问常需科学上网；受众反馈打不开时迁 Cloudflare
  Pages 或阿里云 OSS 香港（站点纯静态，迁移零改动）

## 概念归一化工作流

1. 网页/`query.py similar` 发现同义概念被拆散（如"算力"vs"算力租赁"）
2. 编辑 `data/aliases.json`（键=规范名，值=别名数组；宁可不合并也不错并）
3. `python3 scripts/rebuild_tags.py && python3 scripts/build_site.py`

## 标签层级维护工作流（taxonomy）

- 网页「板块热力」底部的**未入标签库的活跃概念** = 待归类清单
- 编辑 `data/taxonomy.json`：键=父标签，值=子标签数组；父可为虚拟分组
  （如"周期资源"）或真实概念（如"机器人"）；允许多父（飞行汽车∈汽车+低空经济）
- 跑 `python3 scripts/build_site.py` 即生效（纯前端聚合，不动数据库）
- 板块热度=层级内每日**去重**涨停家数；启动判定=近3日均≥3家且≥2.2×前15日基线
