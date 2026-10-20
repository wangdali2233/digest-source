# 约束（给未来的改代码的人 / agent）

本文件优先级高于任何「看起来更方便」的做法。改这个项目之前先读完。

## 一、不可违反

1. **Markdown 是唯一真源。** frontmatter 只用 YAML 基础类型，正文只用 CommonMark。
   不许引入主题特有的 shortcode、标签、语法糖——否则换渲染器时内容全废。
2. **幂等保护不许拆。** 同一天的 `source/_posts/<date>.md` 已存在时必须跳过生成，
   只有 `--force` 才覆盖。拆掉它，bot 的提交会触发 push、push 会重跑管道、
   重跑时 seen.json 已把条目记为已读，于是当天的版面会被一轮轮覆盖坏。
3. **LLM 失败必须回退，不许抛错。** 缺凭据、超时、限流、返回垃圾、JSON 非法，
   一律退回关键词分类并照常出稿。分类是增强，不是依赖。
4. **token 预算（高峰省流）。** 调用 DeepSeek 官方模型时：
   - **高峰时段：北京时间 09:00–12:00、14:00–18:00**（可配置多个区间）。
     必须压低输出：候选 **30 条**、`max_tokens` **800**、摘要 **20 字**。
   - 其余时段（含 12:00–14:00、18:00–次日 09:00）视为错峰：
     候选 60 条、`max_tokens` 4000、摘要 30 字。
   - 实现见 `pipeline/classify.py::budget()`，配置见 `sources.toml` 的 `[llm.budget] peak`。
   - **修改这段逻辑前先问：这会让人在高峰时段多花钱吗？** 会，就别改。
5. **站点仓只放产物。** 构建在源码仓完成，`public/` 单提交 force push 过去。
   不许往站点仓放源码、md、`seen.json`。
6. **摘要只存自己写的一句话，禁止把原文落库。** 抓正文只是为了喂给模型，
   存下来既有版权问题，也会让仓库无限膨胀。单页最多读 `fetch_bytes`，只取
   `excerpt_chars` 进 prompt，落地时只有 summary 字段。
7. **私钥绝不入库。** `site_deploy_key`、`.pem`、任何 `sk-` 开头的字符串。
   已确认历史干净，别把它弄脏。

## 二、改配置前必读的五个坑

这些都是踩过、修好、有验证的：

| 坑 | 正确的写法 |
|---|---|
| `package.json` 缺 `hexo.version` | 必须有 `"hexo": {"version": "7.3.0"}`，否则插件静默不加载、文章静默丢失 |
| `permalink` 用 `:year-:month-:day` | 用 `posts/:title/`，因为 Hexo 的 `:day` 取 UTC，07:00+08:00 会算成前一天 |
| frontmatter 的 `date` 带 `+08:00` | 写成 `2026-10-20 07:00:00`（不带偏移），由 `timezone` 解释 |
| cron 用整点 `0 23 * * *` | 用 `17 23 * * *`。整点最拥堵，实测晚 10–16 分钟甚至卡队列 |
| LLM 请求不关 thinking | 必须 `thinking = "disabled"`。开着思考时 `max_tokens` 会被推理 token 吃光，正文返回空字符串，表现为「返回空 content」而非报错 |

## 三、工作流

```bash
# 改之前：bot 会往 main 提交，永不同步就会分叉
git pull --rebase origin main

# 改之后：两个冒烟必须都过
python3 -m pipeline build --date 2026-10-19 --offline --stdout --no-state
DIGEST_LLM_FAKE=data/fixtures/llm_fake.json \
  python3 -m pipeline build --date 2026-10-19 --offline --stdout --no-state
node -e "require('js-yaml').load(require('fs').readFileSync('.github/workflows/pipeline.yml','utf8'))"

npx hexo clean && npx hexo generate

# 模型清单探测（每两周自动跑，改完也能手动验）
python3 scripts/check_models.py --fixture /tmp/fx/a.json
```

## 四、判断标准

- 出稿宁缺毋滥：条数低于 `min_items` 就中止，不发残缺页。
- 失败要显式：每个源的贡献条数必须打到 stderr，静默归零是最难查的故障。
- 版面要稳定：分节顺序、条目骨架、`<!-- more -->` 的位置都不许随内容波动。
