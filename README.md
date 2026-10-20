# digest-source

每天 07:17 触发（内容窗口到 07:00 为止），抓取信息源、去重、打分、选品，渲染成一篇 Markdown，用 Hexo 构建成静态站点后
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

## LLM 分节

关键词分类在单源（HN）下实测有 64% 的条目落进「其他」，所以加了模型分节：

- **一次请求**把「分数最高的 `llm.limit` 条」（默认 60）分到固定分节表，顺带生成一句话摘要。
- 只送头部候选，不送全部原始条目——token 上限可控。
- **任何失败都退回关键词分类**：没凭据、网络错误、返回垃圾、JSON 解析失败，都只是少了一次增强，
  构建照常出稿。
- 输出严格校验：分节 id 不在表里直接丢，摘要超 80 字直接丢，编号越界直接丢。

### 接哪个模型

只认 **OpenAI 兼容端点**（`/chat/completions`），换服务商只改 `sources.toml` 里三行：

```toml
[llm]
endpoint = "https://api.deepseek.com/chat/completions"
model    = "deepseek-chat"
key_env  = "DEEPSEEK_API_KEY"      # 从哪个环境变量读密钥
```

| 服务 | endpoint | 说明 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/chat/completions` | 默认值，一天成本在厘级 |
| Moonshot | `https://api.moonshot.cn/v1/chat/completions` | 国内直连 |
| 硅基流动 | `https://api.siliconflow.cn/v1/chat/completions` | 有免费额度 |
| 自建 vLLM / Ollama | `http://host:8000/v1/chat/completions` | CI 里需要公网可达 |

> **GitHub Models 已于 2026-07-30 完全下线**，不要再指向 `models.github.ai`——
> 那个域名现在只返回一个 4 字节的 `OK` 健康检查，看起来像成功，实际拿不到任何内容。

分节表里的 `desc` 字段会进 prompt，描述越具体分类越准。

### 高峰省流

调用 DeepSeek 官方模型时，管道按当前时刻自动切换预算。**高峰时段是北京时间 09:00–12:00 与 14:00–18:00**，其余算错峰：

| 时段 | 候选条数 | `max_tokens` | 摘要字数 |
|---|---|---|---|
| 高峰 09:00–12:00、14:00–18:00 | 30 | 800 | 20 |
| 错峰（12:00–14:00、18:00–次日 09:00） | 60 | 4000 | 35 |

定时任务在北京 07:17 触发，正好落在错峰段；手动触发如果赶上高峰，会自动省流。
日志里会打印 `LLM 分类（deepseek-chat，高峰省流，max_tokens=800）` 或 `（…，错峰，…）`。
配置在 `sources.toml` 的 `[llm.budget]`。

### 标签：受控词表

分类之外，同一次调用还会给每条打 **1–3 个标签**，但**只能从受控词表里选**
（`sources.toml` 的 `[tags] vocab`，现在 24 个词）。

为什么要受控：自由打标会立刻退化——`M3.1`、`gsbase`、`Apple Silicon` 这类词每天新增几十个，
标签页会变成几百个只含一条内容的碎页。词表外的一律丢弃，大小写归一，去重后取前 3 个。

- **条目级**：显示在摘要下方，形如 `` `#ai` `#llm` ``，只做展示，不参与导航。
- **文章级**：按当天出现频次取前 8 个，写进 frontmatter 的 `tags`，Hexo 据此生成标签页。

因为是"一天一篇"，标签页的粒度是**天**——`/tags/rust/` 会列出"哪几期提到过 rust"，
而不是"哪篇文章讲 rust"。想要后者必须改成一篇一条目，那是另一个量级的改动。

### 两阶段：分类看标题，摘要看正文

```
阶段一  60 条标题 → 分类 + 粗摘要（一次调用）
阶段二  选中 20 条 → 并发抓原文 → 只取前 1200 字 → 写一句摘要（再一次调用）
```

只给标题时，模型只能把标题翻译一遍（`Mistral发布时间序列基础模型Timestral`）。
喂了正文之后变成：

> Mistral发布首个时间序列基础模型Timestral，**209M 参数**在三大基准居首。
> MiniMax发布M3.1 Flash，**推理提速近三倍、成本降70%**。

抓取策略：并发 8、单页最多 200 KB、超时 12 秒、抽 `article/main` 优先。
抓不到的条目降级为仅标题，不影响其余条目，也不影响出稿。

### 模型清单探测（每两周）

模型名会变（`deepseek-chat` 就是这样悄悄从文档消失的），所以加了一个独立的定时任务
`.github/workflows/model-check.yml`：每月 1 号、15 号各跑一次
`scripts/check_models.py`，调 `GET {base}/models` 拿当前清单，和 `data/models.json`
里的快照对比。有新增、下线，或**配置的模型不见了**，就开一个 issue 提醒，不自动改配置——
换模型要人判断质量。

原则：**继续选低价档**（`-flash`），除非分类质量明显不够用。

**离线验证**（不需要任何凭据）：

```bash
DIGEST_LLM_FAKE=data/fixtures/llm_fake.json \
  python3 -m pipeline build --date 2026-10-19 --offline --stdout --no-state
```

`DIGEST_LLM_FAKE` 指向一个假的模型响应文件，用来验证 prompt 组装、JSON 解析、结果写回这一整条链路。
它覆盖不到真实 API——真实调用以 CI 日志里的 `LLM 分类（模型名）` 为准。

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
- **分类优先走 LLM**，没配 key 时退回关键词 + 域名（单源实测约 64% 会落进「其他」，
  所以 `max_other` 放宽到 4）。
- **摘要有赖于 LLM**。没配 key 时只有标题和元信息，`summary` 字段为空。
- **每天只采集一次**。HN 用 Algolia 的全天窗口查询兜底；若 RSS 源日更量超过 feed 容量，
  需要加一个只做采集、写 `data/raw/` 的高频 workflow。
- **站点仓库每次 force push，只有一个提交**。回滚方式是回到源码仓库对应 commit 重跑，
  提交信息里的 `source@<sha>` 就是映射。
- 依赖锁定在 Hexo 7.3.0 + NexT 8.29.0（这套组合端到端验证过）。Hexo 8 在
  `package.json` 修好后大概率也能跑，但没必要为了追新再冒一次风险。
