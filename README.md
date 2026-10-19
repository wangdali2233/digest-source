# digest-source

每天 07:00 抓取多个信息源，去重、打分、选品，渲染成一篇 Markdown，用 Hexo 构建成静态站点后
推送到 `wangdali2233.github.io` 仓库发布。

- 源码与内容仓库：本仓库（`digest-source`）
- 站点仓库：`wangdali2233.github.io`（只放 Hexo 的 `public/` 产物，Pages 从该仓库 `main` 分支发布）

## 两条硬约束

1. **采集管道只依赖标准库**。HTTP 用 `urllib`，RSS/Atom 用 `ElementTree`，配置用 `tomllib`
   （要求 Python ≥ 3.11）。CI 里不需要 pip 安装任何东西。
2. **Markdown 是唯一真源**。frontmatter 只用 YAML 基础类型，正文只用 CommonMark，
   不用任何主题特有语法——换渲染器时内容文件不用改。

## 本地跑

```bash
npm ci                                  # 或 npm install
# 用内置样本数据，不联网，验证版式
python3 -m pipeline build --date 2026-10-19 --offline --stdout --no-state
# 真实抓取，写入 source/_posts/
python3 -m pipeline build
# 本地预览
npm run server                          # http://localhost:4000
```

`--stdout` 只打印不落盘；`--no-state` 不更新 `data/seen.json`，适合调试；
`--force` 覆盖当天已生成的文件。

## 目录

```
pipeline/
  __main__.py   CLI：串起整条流水线
  config.py     sources.toml 加载
  http.py       带 ETag 条件请求的极简客户端
  fetch.py      hn_algolia / rss 两种抓取器
  normalize.py  URL 归一化、标题分词、Jaccard
  merge.py      归一化 → 去重 → 打分 → 选品
  render.py     → Markdown
  state.py      data/seen.json 跨天去重
source/
  _posts/       每天一篇 YYYY-MM-DD.md
  tags/         标签页（type: tags）
  archives/     归档页（type: archives）
data/
  seen.json     已收录链接的指纹与首末出现日期
  fixtures/     离线演示数据
_config.yml     站点配置
_config.next.yml  主题（NexT）配置
```

## 流水线顺序（不要改）

```
采集 → 归一化 → 跨源去重 → 跨天去重 → 源内百分位归一 → 加权排序 → 分节配额选品 → 渲染
```

先去重再打分，否则同一篇文章的多个来源会互相抬分。
先源内归一再跨源比较，HN 的 300 分和 GitHub 的 star 不是一个量纲。

## 配置（sources.toml）

| 键 | 含义 |
|---|---|
| `site.publish_at` | 出稿时刻，同时是时间窗右边界 |
| `site.window_hours` | 窗口长度，默认 24 |
| `site.window_slack_hours` | 额外向前放宽，防止跑晚了 feed 全被过滤 |
| `quota.daily` | 每期总条数 |
| `quota.per_section` | 每节条数上限 |
| `quota.max_other` | 「其他」桶的独立上限 |
| `quota.min_items` | 低于此数直接中止，宁可停更也不发残缺页 |
| `quota.seen_days` | 同一链接多少天内不重复 |
| `sources[].weight` | 跨源排序权重，调 0 等于静音该源 |
| `sections[].keywords` | 标题关键词 |
| `sections[].domains` | 域名命中，权重高于关键词（arxiv.org 一定是论文） |

## 两个踩过的 Hexo 坑（改配置前先看）

1. **`package.json` 必须有 `"hexo": {"version": "..."}`**。
   缺这个字段时 `env.init` 不会置真，配置和插件会被静默跳过——渲染器不注册、文章不入库，
   日志里只有一行 “Processed”，极难排查。
2. **`permalink` 用 `posts/:title/`，不要用 `:year-:month-:day`**。
   Hexo 的 `:day` 按 UTC 取，`07:00+08:00` 会算成前一天，URL 会错位一天。
   文件名本身就是日期，用 `:title` 直接得到 `/posts/2026-10-19/`。

同样地，frontmatter 里的 `date` 写成不带偏移的本地时间（`2026-10-19 07:00:00`），
Hexo 会按 `_config.yml` 的 `timezone` 解释，避免日期漂移。

## 部署（一次性）

1. **站点仓库开 Pages**：Settings → Pages → Source 选 `Deploy from a branch` → `main` / `(root)`。
2. **生成 deploy key**：
   ```bash
   ssh-keygen -t ed25519 -C "digest-ci" -f ./site_deploy_key -N ""
   ```
   公钥 `site_deploy_key.pub` → 站点仓库 Settings → **Deploy keys** → 勾选 *Allow write access*；
   私钥全文 → 本仓库 Settings → Secrets and variables → Actions → `SITE_DEPLOY_KEY`。
3. 推送本仓库到 `main`，Actions 会跑一次完整流程。手动触发可在 Actions 页面
   `Run workflow`，支持指定日期和 `--force`。

## 已知限制

- **目前只启用 Hacker News 一个源**。加 RSS 源的方式见 `sources.toml` 末尾的注释。
- **分类靠关键词 + 域名**，长尾条目会掉进「其他」。单源实测 47 条里约 64% 落在「其他」，
  所以 `max_other` 放宽到 4；想真正解决，用一次 LLM 调用让模型在固定分节表里选。
- **没有摘要**。`render.py` 已预留 `summary` 字段，接 LLM 后直接填。
- **每天只采集一次**。HN 用 Algolia 的全天窗口查询兜底；若 RSS 源日更量超过 feed 容量，
  需要加一个只做采集、写 `data/raw/` 的高频 workflow。
- **站点仓库每次 force push，只有一个提交**。回滚方式是回到源码仓库对应 commit 重跑，
  提交信息里的 `source@<sha>` 就是映射。
- 依赖锁定在 Hexo 7.3.0 + NexT 8.29.0（这套组合端到端验证过）。Hexo 8 在
  `package.json` 修好后大概率也能跑，但没必要为了追新再冒一次风险。
