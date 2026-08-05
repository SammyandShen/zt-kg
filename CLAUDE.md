# zt-kg — 涨停概念知识图谱

A股每日涨停股 + 涨停原因（概念题材）长周期追踪库。核心用途：拿到最新涨停股票，
快速查它属于什么概念、板块内有哪些股票、板块近期热度，辅助龙头跟涨等短线决策。

**所有输出为行情数据整理，不是投资建议。**

**零人工干预原则（2026-07-25 用户定，系统宪法）**：所有队列由自动工序收敛，
默认不存在"等人工"状态——归因/轮次判决走 judge_attributions（sonnet保守判决+
机械规则），标签分型走 classify_tags+auto_adopt_tags，年报主营走候选自动晋升。
人工只有一个入口：**用户发现 bad case 主动反馈**，修法=OVERRIDES/facts_overrides/
业务JSON 覆盖 + 必要时调紧自动规则。任何新功能不得设计"需人工确认才能推进"的环节；
自动判决必须 decided_by/source 留痕、保守（拿不准 leave 而不是硬判）、可被覆盖。

## 数据真源与职责边界

| 文件 | 谁写 | 说明 |
|---|---|---|
| `data/ztkg.db` | 脚本（fetch_daily/backfill）| SQLite 主库，唯一数据真源 |
| `data/aliases.json` | 人工/LLM（用户确认后）| 概念别名映射，改后必须跑 `rebuild_tags.py` |
| `data/taxonomy.json` | 人工/LLM（用户确认后）| 板块标签层级库（父→子，支持多级/多父），改后跑 `build_site.py` 即生效 |
| `data/tag_meta.json` | gen_tag_meta.py 出草稿+人工修订 | 标签七类分型注册表，热度频道由类型决定 |
| `data/tag_expansions.json` | 人工/LLM（用户确认后）| 一对多语义拆解（事件标签→题材+催化），改后先 `rebuild_tags.py --dry-run` 再实跑 |
| `data/business_facts.json` | 人工核实 | 公司客观业务事实（主营/产品/参股/拟收购），每条必须带证据、成熟度和有效期 |
| `data/event_attributions.json` | 人工核实 | 某股某日的主炒/次要题材归因；不能用来改写公司主营 |
| `data/theme_business_mappings.json` | gen_theme_mappings.py 增量生成（candidate）| 交易题材→客观业务标签**或图谱sector节点**映射；不自动生成涨停归因；转正走印证飞轮（见下） |
| `data/semantic_config.json` | 人工校准 | 语义层阈值（立轮/断轮/首封极差/候选过期/重叠告警）；改后跑 rebuild_semantic_layer.py；校准记录追加 calibration_log |
| `data/facts_overrides.json` | 治理台导出+人工确认 | 人工判决台账（归因核实/轮次成立/龙头认定）；rebuild 每次重放，判决不因候选域重算丢失 |
| `data/similar_dismissed.json` | Claude 判决留痕 | 疑似重复观测口的"已判不并"名单（精确对子+整族正则，如区域国企 `^..国企$`）；判过的不再上榜只露新面孔，verdict_log 记录并/不并理由；bad case 删条目即重新出现 |
| `data/business_tree.json` | gen_business_tree.py（LLM判决留痕） | 孤立 product 业务节点→产业父节点台账；rebuild 以 source='auto_tree' 重放为层级边（manual business_path 优先不覆盖）；parent=null=拿不准留孤儿（--force 重问）；bad case 改 parent 或删条目重问 |
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
- **四段语义闭环**：
  1. `business_concepts` + `business_concept_edges` 是独立的公司业务图谱，
     `stock_business_facts` 回答公司客观做什么；
  2. `event_theme_links` 回答某次涨停按什么题材交易；
  3. `theme_episodes` 回答该题材这一轮从何时启动、当前处于什么阶段。
  4. `theme_business_mappings` 把交易题材映射回业务标签，生成可研究公司池。
  `evidence_items` 及关联表保存证据。由供应商原因自动生成的题材关系必须保持
  `candidate`，自动轮次必须保持 `provisional/closed`；只有人工或后续核验流程才能
  升级为 `verified`。拟收购必须标 `planned_acquisition/proposed`，不得冒充现有主营。
  公司业务概念与交易题材 `concepts` **禁止共用 id 或靠名称隐式连接**；同名“脑机接口”
  可以同时存在于业务命名空间和题材命名空间。产业/细分产品热力只统计
  `core|secondary` 且已商业化的 verified 业务事实；参股、研发、供应链和拟收购只进入
  题材反查候选池，不进入公司在营产业热力。
  库内新闻只有在同时点名股票与具体题材时，才绑定到对应的
  `event_theme_links` 作为题材级旁证并提高候选置信度；仍不得自动升级为 verified。
- **官方主营候选（自动晋升）**：`fetch_company_reports.py` 从巨潮资讯抓取最新完整
  年报，PDF/文本只缓存在 `data/report_cache/`（不提交仓库）；`extract_business_candidates.py`
  只抽取年报明确表述写入 `business_fact_candidates`；rebuild_semantic_layer 内
  conf≥0.7 且 relation=core/secondary 的候选**自动晋升 verified**（source='auto_report'
  留痕，业务概念按名匹配或建独立 product 节点）——年报是主营的权威来源。低置信/
  参股类候选保持 candidate。Claude CLI 不可用时退回确定性规则抽取，
  宁可少抽、不从涨停原因猜主营。bad case 用 business_facts.json 人工条目覆盖。
- **P1 语义层加固**（2026-07-25）：
  - **link_basis 强制凭据**：派生归因必须引用业务边(basis_kind='business_fact')或
    官方公告('announcement')；无凭据=仅盘面联想，置信度封顶0.45，个股页标"无凭据"
  - **轮次硬阈值**（semantic_config.json）：≥3只+同日≥3家+首封极差≤90min 才立轮，
    不达标不造题材；静默≤5交易日保持开放(divergence)，超窗自动关轮；同期开放轮
    成员重叠≥50%打印并轮告警（人工裁决）
  - **退场机制**：候选归因10交易日无轮次归属且无supporting旁证→expired；
    澄清公告点名题材→对应候选直接rejected（corporate_events 澄清击杀通道）
  - **T0快照**：attribution_snapshots 表存候选首次生成点值，重建永不覆盖
    （回测"按当晚归因跟随"的点值正确性基础）；corporate_events=公告派生的公司
    事件记录（8类分型），event 不再是标签词典成员的过渡结构
- **T+归因复核**：`review_theme_attributions.py` 对 candidate 关系按交易日执行 T+0/T+1/T+2
  旁证评分。T+0 看同日题材广度、题材级证据和客观业务映射；T+1 看下一交易日题材广度
  与同股延续；T+2 再看第二个交易日延续。结果写 `attribution_reviews`，只用于人工排序和
  解释，任何分数都不得自动把 `event_theme_links` 升级为 verified。
  **复核表故意无外键**（2026-08-03 事故修复）：原 ON DELETE CASCADE 外键在 rebuild
  删重建 derived 链接时把复核连带删光（每日216条写入→只剩3条）；自然键
  (event_id,concept_id) 跨重建稳定，真孤儿由 rebuild 尾部显式清理。judge 队列
  引用 T+复核结果，此修复后判决才真正看得到旁证。
- **轮次断浪规则（2026-08-03 重构，semantic_config 可调）**：断轮时钟只认活跃日
  （当日≥sustain_min_members=2 只），且续命日广度须≥ceil(本浪峰值×wave_decay_ratio=0.25)
  ——单股散点和相对峰值的涓流尾巴都不续命，断浪后峰值清零，新浪仍要过立轮双门槛。
  修复前任意1只涨停刷新5日时钟，人形机器人/固态电池/国企改革被粘成378天巨轮。
  跨度外零散涨停不归组（走候选过期通道）。起点贴数据窗口开端的轮次导出带
  截断标记（episode 数组 idx9），页面显示"起点早于数据窗口"。
- **龙头/龙二/龙三评分体系（2026-08-05，替换 2026-08-03 的单指标口径）**：
  rebuild 内 `derive_leader_board` 逐轮逐活跃日六维评分→EMA(α=0.5)→逐日
  rank1/2/3 落 `episode_leader_daily`（纯派生，每日 DELETE 全量重算，幂等已验证）。
  六分项（权重 config.leader.weights）：空间.30（0.5绝对板高7板顶+0.5相对当日
  轮内最高）、先手.15（日内首封早+轮内进场身位）、封板质量.20（封成比对数+
  炸板+板型）、正宗度.10（归因置信 verified≥0.85+业务凭据/营收占比加成）、
  隔日溢价.15（该股已实现开盘溢价 vs 轮均，无未来函数）、断板韧性.10
  （daily_kline 断板日跌幅+2活跃日内反包；日K线由 fetch_next_open 顺手落库，
  刷新集合=欠账股∪开放轮成员）。**换龙滞后**：挑战者 EMA 须超现任 25%
  （swap_margin，回测校准）；断板股 resting≤2 活跃日靠记忆撑席位，超时 exited
  让位，再封可复辟；首板不认；龙二/龙三逐日直排无滞后。closed 轮盖棺=rank1
  在位活跃日最多者。market_role='leader' 保留为 rank1 兼容写回（rank2/3 不写）。
  facts_overrides.leaders 覆盖当前榜席位（可带 rank，缺省1；历史时间线不改）。
  **回测**（backtest_leader.py，只读、附权重敏感性扫描）：rank1 隔日开盘超额
  +1.22pct（胜率61%，t≈8.8，735样本）> 旧最高板基线 +1.06pct；单调
  rank1>rank2>rank3；权重±0.10 均在噪声内。前端：主线卡双口径（轮次龙头·当日
  领涨）、轮次面板三席 chips+换龙时间线、治理台③三席⭐覆盖。
- **verified 归因强制证据（2026-08-03）**：apply_link_verdicts 应用 verified 判决时
  把该事件的新闻/公告证据绑为题材级旁证（supporting 优先，至多3条）；一条都绑
  不上的 verified 不落地（保持 candidate 走过期通道）。audit 门禁对**全部** verified
  归因查证据（原来只查 source='manual'）。
- **业务节点自动挂树（2026-08-03）**：年报晋升建的独立 product 节点曾 99% 孤立
  （2392/2416 无父子边，"哪些公司做同一产品/属哪个产业"答不上）。
  `gen_business_tree.py` 每日在发布线以下分批（60/批）把孤立 product 喂 sonnet
  归入产业父节点（优先复用产业池，拿不准留 null），台账 business_tree.json 防重；
  rebuild 重放成 source='auto_tree' 边，产业父节点缺失自动建 sector 节点。
- **映射图谱化+印证飞轮（2026-08-04 阶段一；范围=一年内涨停过的股票，
  不扩全市场——用户明确定界）**：回答"题材对应哪些真实业务、哪些公司具备该业务
  但还没异动"。①映射目标可为图谱 sector 节点（细分/产业）：rebuild 导入时展开为
  后代产品标签行（source='graph' 每日重生成，DO NOTHING 不覆盖直接映射，sector
  级原始行不入库以过门禁），下游按 tag_name 连接的消费端零改动受益——上线时
  (题材,公司)对 298→710（2.4×）；gen_theme_mappings 词表含图谱节点（提示词
  【节点】前缀，"聚合整体贴合才映射"），分批60/批防超时、失败批次台账续传。
  ②印证飞轮 corroborate_mappings：映射被 ≥3 只不同股票的 verified 归因作为业务
  凭据引用→自动转正留痕（每日 rebuild 幂等重算；退场规则等第二层核实量到位再开）。
  ③六层名单 watch 股按"映射已核实 > 营收占比"排序，chip 注"占营收X%"。产业→细分→产品。`gen_business_tree.py
  --refine` 对直挂子产品≥25的根产业各调一次 sonnet 按业内口径聚成3-8个细分
  （台账 groups 段=细分→产业、refined 段=已聚标记防重问，--force 重聚）；
  不贴合任何细分的产品保持直挂。**中间层只允许一级**（groups 的 parent 不得又是
  细分，生成端和 rebuild 导入端双重强制）；细分名不得与根产业/种子重名（防节点
  复用错接两棵枝）。细分是 sector 型节点，落库后自动进日常挂树产业池——新产品
  可直接归细分，大桶不再回涨。前端零改动即兼容：产业榜 primarySectorNodes()
  本就只列无 sector 祖先的根产业（无双计），热力聚合 businessDescendants 走
  传递闭包，细分出现在搜索、交叉透视与概念页上下级 chips 里。
- **互动平台供应链链（③层专用数据源，2026-08-03；双市场）**：`fetch_interactive_qa.py`
  抓公司回复——深市走互动易 irm.cninfo.com.cn（JSON，attachedContent 非空=已回复，
  qaStatus 字段含义不稳定不作依据）；沪市走上证e互动 sns.sseinfo.com（POST
  ajax/getCompany.do data=代码 换公司uid → GET ajax/userfeeds.do type=11 拉已回复
  问答，HTML 片段解析：每 item 末个 m_feed_txt=公司回复、末个中文日期=回复日期）。
  近2日涨停股两市场**交错取满 limit**（按 code 排序会让深市挤掉沪市）。两市场共用
  抽取保险丝：只认词典内 active 的 product/theme/sector 标签 + 方向性供应句式
  （谁向谁供什么，纯共现不算，含否定整句丢弃），产出 relation=supply_chain
  conf=0.55 候选（extractor irm-rule-v1 / sse-rule-v1）；rebuild 以
  **status='candidate'** 落入事实域（source='auto_qa'）——只进六层名单③层与
  题材候选池，永不 verified、不进产业热力。规则极保守，产出以周为单位慢慢攒。
- **revenue_share 营收占比**（2026-08-03）：年报抽取新增可选字段（仅取年报明确
  披露的分部占比，禁止估算），候选→晋升→导出（fact 数组末位 idx11）→个股页
  "占营收X%" 全链贯通；存量事实为 NULL，随后续年报周期增量补齐。
- **凭据率观察哨**（audit_tags 内，2026-08-01 设）：近3交易日归因带凭据率随
  theme→业务映射自动增厚应爬升；2026-08-08 后仍 <20% 则 audit 打警告
  （不阻断发布），提示检查 gen_theme_mappings 桥。
- **轮次判决喂 auto_catalyst**（2026-08-03）：judge 的新轮次队列携带系统自动推导
  的催化摘要，模型任务=核对假设与成员证据一致性（确认/修订→verified；对不上
  →leave；纯标签巧合→rejected），不再要求凭空归纳催化——修复此前判决零产出。
- **隔日开盘溢价（2026-08-05）**：情绪趋势新增两卡——"隔日开盘溢价"（全部/高开股/
  低开股平均，%）与"隔日高开/低开比例"（比例不含平开）。数据链：
  `fetch_next_open.py` 按股增量抓东财历史K线（push2his 免鉴权，**前复权 fqt=1**
  ——相邻K线比值不受除权影响；市场前缀猜错自动换边），对 zt 池事件记
  次一交易日开盘 vs 事件日收盘（=涨停价）落表 event_next_open（停牌顺延到复牌
  首K线；最新交易日的事件次日未开盘，天然待明日）。build_site 按事件日聚合出
  `next_open:{date:[avg,up%,down%,avgUp,avgDown,n]}`；前端 svgChart 已支持负值
  纵轴（零线实线）。17:00 班次在 fetch_daily 后增量跑（早班不跑：07:40 无当日开盘价）。
- **官方事件公告**：`fetch_event_announcements.py` 从巨潮资讯抓取涨停日前后2天内，
  标题命中收购/中标/合同/业绩/批复/投产等催化词的公告，作为 event 级上下文证据。
  只有公告标题或原文明确点名具体题材时才可绑定到 `event_theme_evidence`；官方来源只证明
  公告真实，不自动证明它就是本次涨停原因。

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
python3 scripts/fetch_event_announcements.py --days 2 # 涨停日前后2天巨潮官方公告
python3 scripts/fetch_company_reports.py --days 5 # 为近期涨停股抓取巨潮最新完整年报
python3 scripts/extract_business_candidates.py    # 从年报生成待人工复核的主营候选
python3 scripts/query.py review-business          # 查看主营候选、官方来源与近10日业务缺口队列
python3 scripts/rebuild_tags.py --dry-run      # 预演 aliases/expansions 变更影响（不写库）
python3 scripts/rebuild_tags.py                # aliases/expansions 改后重建派生表
python3 scripts/rebuild_semantic_layer.py --dry-run # 预演公司业务/涨停题材/题材轮次
python3 scripts/rebuild_semantic_layer.py      # 重建语义证据层（保留人工核实记录）
python3 scripts/review_theme_attributions.py --days 5 # 生成T+0/T+1/T+2候选归因旁证
python3 scripts/query.py review-attributions --days 5 # 查看候选归因及每项复核指标
python3 scripts/audit_tags.py                  # 标签质量门禁（只读，不改配置/数据库）
python3 scripts/build_site.py                  # 重导出网页数据
bash install-launchd.sh status                 # 定时任务状态（工作日 07:40 早班 + 17:00 晚班）
bash deploy.sh                                 # 手动发布到 Cloudflare Pages
open docs/index.html                           # 打开交互网页
```

## 对话内查询约定

直接 `sqlite3 data/ztkg.db` 或 query.py 均可。常用 join：
`limit_up_events e ⋈ event_concepts ec ⋈ concepts c`；日期格式 `YYYY-MM-DD`。
其中 `event_concepts` 只能称“历史原因标签”。查询已核实/候选的单次题材应使用
`limit_up_events e ⋈ event_theme_links l ⋈ concepts c`；公司业务使用
`stock_business_facts`，题材轮次使用 `theme_episodes`；题材反查业务公司池使用
`theme_business_mappings ⋈ stock_business_facts`，候选池不等于本轮已炒作成分。

## 公网部署

- **线上地址：https://sammyandshen.github.io/zt-kg/**（GitHub Pages，
  仓库 SammyandShen/zt-kg 公开，main 分支 /docs 目录，含 .nojekyll）
- deploy.sh = git add/commit/push；`.deploy-enabled` 存在时定时班次自动发布
  （该开关文件在 .gitignore 里，删掉即停）
- **双班次**（2026-08-05 用户定：昨日涨停信息必须在次日 9 点前补齐，不等 17:00）：
  - 早班 run-morning.sh 工作日 07:40——隔夜公告/互动回复/新闻抓取 → 年报候选
    提取+晋升 → judge 判决 → rebuild+发布；不抓当日涨停池、不跑分型/映射/挂树慢工序
  - 晚班 run-daily.sh 工作日 17:00——当日涨停池+全量语义链+发布线后慢速富化；
    富化产生的候选由次日早班晋升（晚班内 rebuild 在提取之前，属既定顺序）
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

## 标签七类分型模型（tag_meta.json）

- **词典三文件为最终形态**（2026-07-26 定）：tag_meta.json(分型) + taxonomy.json
  (层级) + aliases.json(同义词) 各司其职；曾计划合并为单一 tags.json，评估后取消
  ——频道一致性门禁/映射文件/唯一性校验已覆盖合并的实际收益，重写全部读写层
  的回归风险大于收益。**event 类型已出词典**：event 是记录不是词汇，公告类事件
  走 corporate_events 表，原始原因永存 reason_type；存量 event 标签已由
  migrate_event_tags.py 整体 retire，gen_tag_meta 新判 event 的照常登记但
  auto_adopt 不会转正
- 类型：sector(稳定大产业)/product(稳定细分行业、产品、技术或业务线)/
  theme(交易题材)/catalyst(催化)/attribute(属性)/event(一次性事件→仅过渡分型，
  终态一律 retired)/unknown
- 频道由类型直接决定：sector|product|theme→题材热力+启动榜；
  catalyst→催化热力；其余不进热度。`virtual=true` 表示只做聚合导航的虚拟分组
- 边界：产业回答“长期做什么”，题材回答“市场当前交易什么”；产品/材料/零部件
  默认 product，不得为了进热力硬判 sector/theme。`XX供应商/客户`是 attribute，
  `XX产业链/XX概念`是 theme
- status：active(已审核并进入正式体系) / candidate(已初分型、待热力准入或进一步复核，
  不进正式热度) / retired。正式热力还会二次校验：节点及后代必须 active 且与根同频道
- 新标签达标后 `python3 scripts/gen_tag_meta.py` 增量出草稿（不覆盖已审条目），
  `python3 scripts/query.py review-tags` 看待审清单，人工改 type/status 后跑 build_site.py
- `gen_tag_meta.py --all`：全库长尾一并登记（模式命中给建议类型、无命中=unknown，
  一律 candidate）——已跑过一次实现全覆盖，日常增量跑不带 --all 即可
- `classify_tags.py`：claude CLI(sonnet) 复核分型，范围=candidate+active
  （OVERRIDES 受保护；active 改判门槛 conf≥0.8 并列明细）。台账
  data/llm_review.json 断点续传；父节点建议存 llm_parent_suggestions.json
  不自动改树、不得因父节点建议直接把 candidate 转 active。新标签积累后重跑即可只判增量
- 网页「标签中心」(#/tags)三页签：
  - **题材全景**：taxonomy 全树 + 生命周期徽章（休眠/活跃/启动/高潮/退潮，由热度
    曲线自动判）+ 热度条；点节点看该枝梯队表（空间板/中军/首板/触及 + 晋级率）
    与分枝热度；节点可 ✎编辑 / ➕挂子标签 / ➕新建虚拟分组
  - **全局筛选**(#/filter 兼容)：四类型行多选（行内=或、跨行=且）+ 时间窗/连板/
    触及/交易所组合筛股
  - **治理台**：**核实队列只开三个口**（①活跃轮次主炒归因-近2日 ②新轮次提案-近3日
    ③龙头认定；判决暂存 localStorage → 导出判决 → 合并 facts_overrides.json →
    rebuild 重放；其余候选保持灰显永不进队列、不追求清零）+ 覆盖率与未入树股票、
    标签待审队列（LLM建议一键采纳/批量设类型）、共现观测。**疑似重复观测口已
    退役（2026-08-03）**：子串启发式产出几乎全是层级词/时间变体，真同义仅8对已
    全部并入 aliases；同义发现改走 query.py similar（Claude 维护时跑）+ bad case，
    判决审计记录在 data/similar_dismissed.json
  - **判决队列由 Claude 代行**（2026-07-25 用户授权，用户不做人工判决）：
    `judge_attributions.py` 每日在 run-daily 内自动跑——①归因②新轮次走 sonnet
    保守判决（verified需可解释关联+当日叙事主导；rejected需明确反证；拿不准一律
    leave等过期），判决写 facts_overrides.json 带 decided_by=llm-sonnet 留痕，
    已判键幂等跳过。③龙头已出判决队列（2026-08-03）：由 rebuild 每日自动评分
    产出（见"龙头/龙二/龙三评分体系"），治理台③卡展示开放轮次三席榜面，
    席位按钮输代码即 bad case 覆盖（补丁带 rank）
- **轮次分层名单**（概念页轮次卡下方，核心视图）：本轮跟随股按关系成色六层——
  ①核心业务(主营/重要收入) ②直接成长(有产品收入小/在研) ③上下游映射(supply_chain)
  ④参股布局(holding) ⑤拟收购转型(planned_acquisition) ⑥纯市场联想(无业务证据)；
  实心=本轮已涨停(trading)、虚线=有业务未异动(watch)。入口：热力页主线卡一键直达、
  题材pill点名字、概念页直接访问。层级由 relation/maturity 机械映射，数据薄时中间层空
- 复盘页最新日顶部有「标签库日报」卡：覆盖率/未入树/待审积压/🆕新面孔（库内首次涨停）
- 热力页为四段式（P0 改版）：①市场状态条（涨停/封板率/最高连板/归因覆盖/业务
  覆盖，带环比箭头）②今日主线（最新日题材归因 Top3 大卡：生命周期徽章+家数Δ+
  续板率+领涨股+10日火花线）③变化面（🔥加速中=动量≥1.6 / 🌱新启动=近3日有量且
  前15日≈0 / 🌊退潮中=动量≤0.55，只看题材层）④结构面（三层排行降级折下，产业/
  产品列业务覆盖<30%时整体灰化+⚠芯片）+交叉透视（搜索框在卡头）+产业轮动 bump+
  三层热力表+爆发预警。**口径注册表 METRIC_DEFS**（index.html）是全站指标定义唯
  一来源，页面一律用 ⓘ 悬停展示，禁止再写死定义卡；新指标先进注册表再上页面
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
- taxonomy 只允许 active 节点，父子必须同频道（sector/product/theme 共用题材频道，
  catalyst 只挂 catalyst；attribute/event 不进热力树）。题材下不得直接挂大产业，
  通用产品不得仅因一次共现挂进特定题材；`build_site.py` 遇到违规会直接失败
- 改完先跑 `python3 scripts/audit_tags.py`，看到 `✅ 标签质量门禁通过` 后再跑
  `python3 scripts/build_site.py`
- 板块热度=层级内每日**去重**涨停家数；启动判定=近3日均≥3家且≥2.2×前15日基线
