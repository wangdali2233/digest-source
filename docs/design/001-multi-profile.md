# 001 · 多栏目改造设计（每日信息）

| | |
|---|---|
| 状态 | **已实施**（2026-10-20 评审通过） |
| 作者 | agent |
| 日期 | 2026-10-20 |
| 影响范围 | 配置结构、URL、调度、站点导航 |
| 预计工期 | 1 天（分三步，每步可独立验收） |

---

## 0. 流程约定（本次起长期有效）

从本文档开始，**所有结构性改动先出设计文档，评审通过后才动代码**。

- 文档放在 `docs/design/`，编号递增：`001-multi-profile.md`、`002-xxx.md`
- 文档必须包含：目标、非目标、现状、方案、验收标准、风险与回滚
- 评审通过前不提交业务代码；文档本身可以单独提交

---

## 1. 目标

把单栏目的「技术信息聚合」改成多栏目的「每日信息」：

| 栏目 | category | 状态 |
|---|---|---|
| 技术信息聚合 | `tech` | 现有内容迁移过来 |
| 财经 | `finance` | 新增 |

站点标题改为 **每日信息**，导航：技术 / 财经 / 归档 / 标签。

## 2. 非目标（明确不做）

- 不做插件系统（选 profile 方案，2 个栏目不构成插件化的收益）
- 不做数据卡片 / 时间线 / 周报等非列表形态
- 不分站，所有栏目共用一个 Hexo 站点、一份 `seen.json`、一套部署流程
- 不引入新的第三方依赖（仍然只用标准库）

## 3. 现状与问题

现在的代码有四处假设了「只有一个栏目」：

| 位置 | 现状 | 加栏目后 |
|---|---|---|
| `pipeline/__main__.py` | 输出 `<date>.md` | 两个栏目同日撞文件名 |
| 同上 | 幂等按「文件存在」判断 | 栏目 A 生成后，栏目 B 被误判为已存在 |
| `pipeline/render.py` | `categories: ["daily"]` 写死 | 所有文章同一个分类 |
| `sources.toml` | sections / sources / quota 全局唯一 | 财经不能有自己的分节和配额 |

## 4. 目标结构

```
profiles/
  tech.toml        技术信息聚合
  finance.toml     财经
pipeline/          核心管道（采集/归一化/去重/打分/渲染）
  profile.py       新增：加载并校验 profile
source/_posts/
  tech/2026-10-20.md
  finance/2026-11-01-morning.md
  finance/2026-11-01-evening.md
data/seen.json     两个栏目共用
```

命令行：`python3 -m pipeline build --profile tech [--date ...] [--force]`，
`--profile` 默认 `tech`，保持现有调用方式可用。

## 5. Profile 合约

一个 profile 就是一份声明，不含逻辑：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` / `title` / `category` | 是 | 目录名、导航名、Hexo category |
| `publish_at` | 是 | 内容窗口右边界（本地时间） |
| `window_hours` | 是 | 窗口长度 |
| `sources` | 是 | 源清单，复用现有 `hn_algolia` / `rss` |
| `sections` | 是 | 分节表（id / title / desc / keywords / domains） |
| `tags.vocab` | 否 | 额外词表，叠加在核心词表上 |
| `rank` | 是 | `percentile`（科技）或 `llm`（财经） |
| `quota` | 是 | daily / per_section / min_items / max_other |
| `summary` | 否 | 摘要策略覆盖（如财经「不复述数字」） |
| `footer` | 否 | 文末固定文案（免责声明） |

核心负责：HTTP、归一化、去重、渲染骨架、幂等、发布、观测。
Profile 只决定：**收什么、怎么选、怎么分节、什么时候发**。

## 6. URL 与路径（已确认方案 A）

目录三层（分类 / 年 / 月），文件名带日：

```
/tech/2026/10/19.html
/tech/2026/10/20.html
/finance/2026/11/01-morning.html
/finance/2026/11/01-evening.html
```

**实现方式**：Python 按本地日期算出路径，写进 frontmatter 的 `permalink`，
不使用 Hexo 的 `:year/:month` 变量。

原因（已验证）：`2026-11-01 05:30` 北京 = `2026-10-31 21:30` UTC，
Hexo 按 UTC 取年月会得到 `2026/10`，财经晨报**每个月最后一天都会错月**。

**月归档说明**：目录层级不产生可浏览页面，`/finance/2026/11/` 会 404。
按月浏览仍走 `/archives/2026/11/`（Hexo 自带）。

**旧文章**：2 篇从 `/posts/2026-10-19/`、`/posts/2026-10-20/` 迁到
`/tech/2026/10/19.html`、`/tech/2026/10/20.html`。旧链接失效，不做重定向
（站点上线不足一天，且有外部引用的可能性极低）。

## 7. 财经栏目

| 项 | 值 | 理由 |
|---|---|---|
| 源 | 美联储公告 + CNBC | 实测可用；官方源零噪音，CNBC 提供主流叙事 |
| 窗口 | 12h | 与两次发布对齐，不重叠不重复 |
| 发布 | 05:30 晨报 / 17:30 晚报 | 覆盖美股全天 + 亚洲时段 |
| 分节 | macro / markets / companies / policy | 不设「其他」，财经宁可少收 |
| 每期条数 | 6 | 信噪比低，挑比科技更严 |
| `min_items` | 1 | 一条重要消息就值得成期；0 条则跳过不发布 |
| 排序 | `llm` | 40 条挑 6 条正是模型强项，且本来就要调一次 LLM |
| 摘要 | 只转述结论，不复述数字 | 数字写错是事故 |
| 文末 | 「仅供信息参考，不构成投资建议」 | 合规 |

## 8. 标签方案

**词表分层**

- 核心词表（两栏目共用，24 个）：`ai llm agents oss release rust python javascript database infra essay security linux web hardware science policy startup business design productivity graphics video privacy mobile`
- 财经扩展（8 个）：`rates earnings cbank fx commodities inflation trade regulation`

**三层限制**

| 参数 | 值 |
|---|---|
| 每条标签数 | ≤ 3 |
| 每篇 frontmatter 标签 | ≤ 8（分节优先并入） |
| 标签页总数 | 词表封顶 ≈ 32 + 8 分节 = 40 页，不随文章数增长 |

**跨栏目聚合**：Hexo 标签是全局的，`/tags/policy/` 会同时列出技术期和财经期，无需额外开发。

**遗留规则**（暂不实现）：等积累 20 期后统计，覆盖率 >70% 的标签等于「全部文章」，
应从词表降级。不提前做，避免过早优化。

## 9. 去重与幂等

| 机制 | 规则 |
|---|---|
| 幂等键 | `source/_posts/<profile>/<date>[-slot].md` 是否存在 |
| `--force` | 语义不变：忽略当天 seen 记录，重新生成 |
| `seen.json` | 两个栏目共用一份；记录 `first / last / max_points / profiles` |
| 同一链接 | 不同栏目**各收录一次**；同一栏目 7 天内不重复 |

`profiles` 字段是新增的，用于实现「各栏目各一次」。存量记录没有该字段，
按「已在本栏目出现过」处理（保守，不会导致重复投放）。

## 10. 调度

| 任务 | cron (UTC) | 北京时间 |
|---|---|---|
| 技术 | `17 23 * * *` | 07:17 |
| 财经晨报 | `30 21 * * *` | 05:30 |
| 财经晚报 | `30 9 * * *` | 17:30 |

三个独立 workflow 文件（`publish-tech.yml`、`publish-finance-am.yml`、
`publish-finance-pm.yml`），每个约 30 行，共用同一套步骤。

- 选独立文件而非复用 workflow：GitHub 的 reusable workflow 需要额外一层，
  3 个任务下收益小于复杂度
- **栏目超过 3 个时改为 dispatcher**：一个 workflow 每 15 分钟跑，判断哪些栏目到期。
  届时再改，本文档只记录这个阈值

## 11. 实施步骤与验收

### 步骤 1a · 抽象 profile，路径不变

- 抽出 `profiles/tech.toml`，管道支持 `--profile`
- **URL 暂时不变**（仍是 `/posts/2026-10-20/`）
- 验收：用 2026-10-19 / 2026-10-20 的数据跑，生成的 md 与仓库现有文件**逐字节一致**

```
python3 -m pipeline build --profile tech --date 2026-10-19 --offline --stdout --no-state
diff <(git show HEAD:source/_posts/2026-10-19.md) <(上一步输出)
```

### 步骤 1b · 切路径

- 文件名改 `source/_posts/tech/<date>.md`
- frontmatter 写入 `permalink: tech/YYYY/MM/DD`
- 验收：本地 `hexo generate` 后产物路径为 `public/tech/2026/10/19/index.html`

### 步骤 2 · 加入财经

- 新增 `profiles/finance.toml`
- 新增 `llm` 选品策略（40 条挑 6 条）
- 验收：一次手动运行能出 1 期，含摘要、分节、标签、免责声明

### 步骤 3 · 站点改造

- 站点标题 `每日信息`，导航加 技术 / 财经
- 旧文章补 `categories: ["tech"]`
- 验收：线上三个栏目页面可访问，标签页跨栏目聚合正常

## 12. 风险与回滚

| 风险 | 影响 | 处置 |
|---|---|---|
| URL 变更 | 2 篇旧链接失效 | 已确认接受；如需要可后补重定向页 |
| 两栏目写同一 `seen.json` 并发冲突 | 后写覆盖先写 | 两个 workflow 时间错开 2 小时以上；冲突时以 `max_points` 取大 |
| 财经源可达性 | 抓不到 | 已在本地实测 200 且返回合法 RSS；失败时保留「本期缺失源」机制 |
| profile 抽象出错 | 输出变化 | 步骤 1a 用逐字节对比兜底，不一致就回滚 |

回滚方式：`git revert` 对应提交 + 手动触发一次发布。

## 13. 待确认

| # | 问题 | 默认值 |
|---|---|---|
| 1 | 财经晨报 05:30 / 晚报 17:30 | 按此执行 |
| 2 | 财经每期 6 条 | 按此执行 |
| 3 | 旧文章 URL 变更可接受 | 按此执行 |
| 4 | 摘要「不复述数字」的严格执行程度 | 先写进 prompt 引导，不做代码校验 |
