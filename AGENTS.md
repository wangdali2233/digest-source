# 约束（给未来的改代码的人 / agent）

本文件优先级高于任何「看起来更方便」的做法。改这个项目之前先读完。

## 一、不可违反

1. **结构性改动先出设计文档。** 新功能、改配置结构、改 URL、改调度，都先在
   `docs/design/` 写一份编号文档（`001-xxx.md`），评审通过后才动代码。
   文档必须包含：目标、非目标、现状、方案、验收标准、风险与回滚。
2. **Markdown 是唯一真源。** frontmatter 只用 YAML 基础类型，正文只用 CommonMark。
   不许引入主题特有的 shortcode、标签、语法糖——否则换渲染器时内容全废。
3. **幂等保护不许拆。** 同一天（含时段）的 `source/_posts/<profile>/<YYYY>/<MM>/<DD>[-slot].md`
   已存在时必须跳过生成，只有 `--force` 才覆盖。拆掉它，bot 的提交会触发 push、
   push 会重跑管道、重跑时 seen.json 已把条目记为已读，当天的版面会被一轮轮覆盖坏。
4. **LLM 失败必须回退，不许抛错。** 缺凭据、超时、限流、返回垃圾、JSON 非法，
   一律退回关键词分类 / 按时间选品，并照常出稿。LLM 是增强，不是依赖。
5. **token 预算（高峰省流）。** 调用 DeepSeek 官方模型时：
   - **高峰时段：北京时间 09:00–12:00、14:00–18:00**（可配置多个区间）。
     必须压低输出：候选 **30 条**、`max_tokens` **800**、摘要 **20 字**。
   - 其余时段（含 12:00–14:00、18:00–次日 09:00）视为错峰：
     候选 60 条、`max_tokens` 4000、摘要 35 字。
   - 实现见 `pipeline/classify.py::budget()`，配置见 `config.toml` 的 `[llm.budget] peak`。
   - **修改这段逻辑前先问：这会让人在高峰时段多花钱吗？** 会，就别改。
6. **站点仓只放产物。** 构建在源码仓完成，`public/` 单提交 force push 过去。
   不许往站点仓放源码、md、`seen.json`。
7. **摘要只存自己写的一句话，禁止把原文落库。** 抓正文只是为了喂给模型，
   存下来既有版权问题，也会让仓库无限膨胀。单页最多读 `fetch_bytes`，只取
   `excerpt_chars` 进 prompt，落地时只有 summary 字段。
8. **定时任务必须有补跑，且补跑不能跨本地日。** GitHub 的计划任务是 best-effort，
   实测出现过晚 2.5 小时、甚至整次不触发。每个触发器配 2–3 个 cron，靠幂等保护跳过已生成的。
   **所有补跑时间点必须落在同一个本地日内**——跨过午夜会按新日期生成，产出错误内容。
9. **私钥绝不入库。** `site_deploy_key`、`.pem`、任何 `sk-` 开头的字符串。
   已确认历史干净，别把它弄脏。

## 二、改配置前必读的六个坑

这些都是踩过、修好、有验证的：

| 坑 | 正确的写法 |
|---|---|
| `package.json` 缺 `hexo.version` | 必须有 `"hexo": {"version": "7.3.0"}`，否则插件静默不加载、文章静默丢失 |
| `permalink` 用 `:year-:month-:day` | 用 frontmatter 写死 `tech/2026/10/19.html`（`pipeline/render.py::_permalink`）。Hexo 的 `:year/:month/:day` 全按 UTC 取，05:30 的稿子月末会直接错月 |
| frontmatter 的 `date` 带 `+08:00` | 写成 `2026-10-20 07:00:00`（不带偏移），由 `timezone` 解释 |
| cron 用整点 `0 23 * * *` | 用 `17 23 * * *` 这类非整点。整点最拥堵，实测晚 10–16 分钟甚至卡队列 |
| LLM 请求不关 thinking | 必须 `thinking = "disabled"`。开着思考时 `max_tokens` 会被推理 token 吃光，正文返回空字符串，表现为「返回空 content」而非报错 |
| `new_post_name` 不含 `:title` | 用默认 `:title.md`。Hexo 会用它从文件名解析 slug，缺失时 `data.slug` 被覆盖成 undefined，文章静默不入库 |

## 三、目录约定

```
config.toml            全局：站点、输出、LLM、核心词表
profiles/<id>.toml     栏目：发布时刻、窗口、分节、源、配额、排序策略
pipeline/              核心管道（采集/归一化/去重/渲染/发布）
docs/design/           设计文档，编号递增
source/_posts/<id>/<YYYY>/<MM>/  按栏目/年/月分层，文件名只留「日」
data/seen.json         两个栏目共用
```

新增栏目 = 新增一份 `profiles/<id>.toml` + 一个 `publish-<id>.yml` 触发器。
**不需要改核心代码**；如果需要改核心，说明抽象层设计错了，先回头看设计文档。

## 四、工作流

```bash
# 改之前：bot 会往 main 提交，永不同步就会分叉
git pull --rebase origin main

# 技术栏目
python3 -m pipeline build --profile tech --date 2026-10-19 --offline --stdout --no-state
DIGEST_LLM_FAKE=data/fixtures/llm_fake.json \
  python3 -m pipeline build --profile tech --date 2026-10-19 --offline --stdout --no-state

# 财经栏目（必须带 --slot）
DIGEST_LLM_FAKE=data/fixtures/llm_fake.json \
  python3 -m pipeline build --profile finance --slot morning --date 2026-10-21 --offline --stdout --no-state

# 结构校验
node -e "const y=require('js-yaml'),f=require('fs');for(const p of f.readdirSync('.github/workflows'))y.load(f.readFileSync('.github/workflows/'+p,'utf8'))"
npx hexo clean && npx hexo generate

# 模型清单探测（每两周自动跑）
python3 scripts/check_models.py --fixture /tmp/fx/a.json
```

## 五、判断标准

- 出稿宁缺毋滥：条数低于 `min_items` 就中止，不发残缺页。
- 失败要显式：每个源的贡献条数必须打到 stderr，静默归零是最难查的故障。
- 版面要稳定：分节顺序、条目骨架、`<!-- more -->` 的位置都不许随内容波动。
- 抽象对不对，用「加栏目要不要改核心代码」来检验。
