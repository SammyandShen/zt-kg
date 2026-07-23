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
| `data/tag_meta.json` | gen_tag_meta.py 出草稿+人工修订 | 标签六类分型注册表，热度频道由类型决定 |
| `data/tag_expansions.json` | 人工/LLM（用户确认后）| 一对多语义拆解（事件标签→题材+催化），改后先 `rebuild_tags.py --dry-run` 再实跑 |
| `docs/data.js` | 脚本 `build_site.py` | **禁止手改** |
| `docs/index.html` | 人工维护 | 单页应用（file:// 直接打开即可用） |

- `limit_up_events.reason_type` 永存原始涨停原因；`event_concepts` 是派生表，
  可随时由 `rebuild_tags.py` 全量重建，概念合并规则可无限迭代。
- 数据源：同花顺涨停池 API（**约1年滚动窗口**，更早日期报"date参数不合法"）。
  二期扩3年需接问财/开盘啦。多源防双计双保险：入库按日选源 +
  build_site 按 SOURCE_PRIORITY（ths>wencai>kpl）每(日,股)只导出一条。
- **双池**：涨停池(limit_up_pool, pool='zt') + 炸板池(open_limit_pool, pool='touch'
  触及涨停未封住)。炸板记录无 reason/连板数，不建概念关系、不进热度/新闻/归因，
  网页各处 ⚡虚线样式区分展示。fetch_log 里炸板池记账 source='ths:touch'。
- AGENTS.md 是 CLAUDE.md 的软链接（Codex CLI 等其他agent共用同一份约定，勿单独编辑）。
- `lb_count`（连板数）由 high_days_value 解码：`value >> 16` = 板数。
- 新闻关联：东财搜索接口按股票名逐只查，取 [涨停日-2, +1] 窗口内新闻，
  标题点名的优先、榜单类降权，每次最多存3条（URL去重累积）。
  **成熟度规则**（news_log/briefs 共用）：上次抓取/生成时间早于「涨停日+2天」
  = 窗口未关闭 = 不成熟，--days 覆盖到就自动重查/重算；之后为终态永久跳过。
  所以 run-daily --days 2 能真正补入当晚和次日的解读稿并刷新归因。
  每股查两次（时间序+相关性序合并去重），防止高曝光股被大盘综述刷屏、
  涨停当晚点名稿挤出窗口。summarize 取材按「标题点名优先、新的优先」排序。
  data.js 只导出最近60个交易日的新闻（控体积）。
- LLM一句话归因（summarize_news.py）：调本机 claude CLI 无头模式（走订阅无API费），
  haiku 模型，每交易日一次调用打包全部涨停股，输出 JSON 存 briefs 表。
  提示词要求自然语句禁止"+"串标签。claude 不在 launchd PATH 时脚本自动找
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
python3 scripts/rebuild_tags.py --dry-run      # 预演 aliases/expansions 变更影响（不写库）
python3 scripts/rebuild_tags.py                # aliases/expansions 改后重建派生表
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
- UI 主题：字节跳动风格蓝绿（--accent 蓝 #2e6be6 / --teal 青绿 #00b6a1），
  红色 --up 只保留涨停语义（连板徽章/🔥热度/涨跌箭头）；题材热力蓝、催化热力青绿
- 注意：github.io 大陆访问常需科学上网；受众反馈打不开时迁 Cloudflare
  Pages 或阿里云 OSS 香港（站点纯静态，迁移零改动）

## 概念归一化工作流

1. 网页/`query.py similar` 发现同义概念被拆散（如"算力"vs"算力租赁"）
2. 编辑 `data/aliases.json`（键=规范名，值=别名数组；宁可不合并也不错并）
3. `python3 scripts/rebuild_tags.py && python3 scripts/build_site.py`

## 标签六类分型模型（tag_meta.json）

- 类型：sector(大产业)/theme(题材)/catalyst(催化)/attribute(属性)/event(一次性事件)/unknown
- 频道由类型直接决定：sector|theme→题材热力+启动榜；catalyst→催化热力；其余不进热度
- status：active(已审核，进热度) / candidate(待审核，只在"未审核新热点"区按共振展示) / retired
- 新标签达标后 `python3 scripts/gen_tag_meta.py` 增量出草稿（不覆盖已审条目），
  `python3 scripts/query.py review-tags` 看待审清单，人工改 type/status 后跑 build_site.py
- `gen_tag_meta.py --all`：全库长尾一并登记（模式命中给建议类型、无命中=unknown，
  一律 candidate）——已跑过一次实现六类全覆盖（2420条），日常增量跑不带 --all 即可
- `classify_tags.py`：claude CLI(sonnet) 复核分型，范围=candidate+active
  （OVERRIDES 受保护；active 改判门槛 conf≥0.8 并列明细）。台账
  data/llm_review.json 断点续传；父节点建议存 llm_parent_suggestions.json
  不自动改树。已全量跑过一次（2405条判定）；新标签积累后重跑即可只判增量
- 网页「标签中心」(#/tags)三页签：
  - **题材全景**：taxonomy 全树 + 生命周期徽章（休眠/活跃/启动/高潮/退潮，由热度
    曲线自动判）+ 热度条；点节点看该枝梯队表（空间板/中军/首板/触及 + 晋级率）
    与分枝热度；节点可 ✎编辑 / ➕挂子标签 / ➕新建虚拟分组
  - **全局筛选**(#/filter 兼容)：四类型行多选（行内=或、跨行=且）+ 时间窗/连板/
    触及/交易所组合筛股
  - **治理台**：覆盖率与未入树股票、待审核队列（带 LLM 建议置信度，可一键采纳/
    批量设类型）、疑似重复标签、共现观测（↑=较前10日上升，新叙事信号）
- 复盘页最新日顶部有「标签库日报」卡：覆盖率/未入树/待审积压/🆕新面孔（库内首次涨停）
- 热力页有「题材轮动」bump 图（Top8 根题材滚动5日热度排名）；未审核区只留
  共振≥3 的爆发预警，完整队列在治理台
- build_site 导出 llm_sugg（candidate 标签的 LLM 建议）供治理台展示
- **网页手动分类入口**：热力页"未审核新热点"每个标签带 ✎、概念详情页标题带 ✎，
  弹层选类型/状态/父节点存 localStorage 本地立即生效；右下角"导出补丁"生成 JSON
  （tag_meta合并 + taxonomy追加），发给 Claude 或手工合并落盘后跑 build_site.py，再清空本地标注
- 原则：别名只做严格同义词（属性≠题材，"国企"曾被错并进"国企改革"已纠正）；
  父子关系进 taxonomy；原始 reason_type 永不删改
- **一对多拆解（tag_expansions.json）**：复合事件标签拆成 题材+催化，如
  "拟收购存储公司"→存储芯片+并购重组、"半导体级氢氟酸涨价"→氢氟酸+产品涨价。
  归一化总管线 = 拆分 →（命中展开键则）展开 → 别名归一（common.normalize_tags，
  入库与重建共用）。展开键不得与 aliases 重叠、不得链式；raw_tag 永远记原始写法。
  拆解后的源标签从 taxonomy 移出、tag_meta 标 retired。改配置流程：
  编辑 json → `rebuild_tags.py --dry-run` 过目（展开命中/概念增减/计数变化）→ 实跑 → build_site

## 标签层级维护工作流（taxonomy）

- 网页「板块热力」底部的**未入标签库的活跃概念** = 待归类清单
- 编辑 `data/taxonomy.json`：键=父标签，值=子标签数组；父可为虚拟分组
  （如"周期资源"）或真实概念（如"机器人"）；允许多父（飞行汽车∈汽车+低空经济）
- 跑 `python3 scripts/build_site.py` 即生效（纯前端聚合，不动数据库）
- 板块热度=层级内每日**去重**涨停家数；启动判定=近3日均≥3家且≥2.2×前15日基线
