"""第二阶段：抓正文 → 一次 LLM 调用写摘要。

和分类阶段的区别
----------------
* 分类只看标题，要的是「分到哪一节」；
* 摘要要看正文，否则模型只能把标题翻译一遍（这是实测出来的结论）。

三条硬约束
----------
1. **只存自己写的一句话，不存原文。** 版权和仓库体积都不允许搬运全文。
2. **抓不到就降级**：单个链接失败只影响那一条的摘要质量，不影响出稿。
3. **失败不抛错**：LLM 挂了就整期不带摘要，照常发布。
"""

from __future__ import annotations

import concurrent.futures
import html as htmllib
import re
import urllib.request

from . import classify

UA = "digest-bot/1.0 (+https://github.com/wangdali2233/digest-source)"

_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|nav|header|footer|aside|form|svg|iframe)\b[^>]*>.*?</\1>",
    re.S | re.I,
)
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_MAIN = re.compile(r"<(article|main)\b[^>]*>(.*?)</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def extract_text(html: str, limit: int = 1200) -> str:
    """粗暴但够用的正文抽取：优先 article/main，去掉脚本样式，压平空白。"""
    html = _COMMENT.sub(" ", html)
    html = _DROP_BLOCKS.sub(" ", html)
    match = _MAIN.search(html)
    body = match.group(2) if match else html
    text = htmllib.unescape(_TAGS.sub(" ", body))
    text = _WS.sub(" ", text).strip()
    return text[:limit]


def fetch_article(url: str, timeout: int, max_bytes: int) -> str | None:
    request = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and "html" not in ctype and "text" not in ctype:
            return None
        raw = resp.read(max_bytes)
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def gather(items: list[dict], conf: dict) -> dict[str, str]:
    """并发抓取，返回 {item_id: 正文摘录}。失败的映射为空字符串。"""
    timeout = int(conf.get("timeout", 12))
    max_bytes = int(conf.get("fetch_bytes", 200_000))
    limit = int(conf.get("excerpt_chars", 1200))

    def work(item: dict) -> tuple[str, str]:
        try:
            page = fetch_article(item["url"], timeout, max_bytes)
            return item["id"], extract_text(page, limit) if page else ""
        except Exception:  # 403、超时、DNS、非法编码，全都当抓不到
            return item["id"], ""

    out: dict[str, str] = {}
    workers = max(1, int(conf.get("workers", 8)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for item_id, text in pool.map(work, items):
            out[item_id] = text
    return out


def _build_prompt(items: list[dict], excerpts: dict[str, str], chars: int) -> str:
    lines = [
        "你是技术信息聚合的编辑。为下面每一条写一句中文摘要。",
        "",
        "要求：",
        f'1. 只输出 JSON，形如 {{"items":[{{"i":0,"summary":"…"}}]}}',
        f"2. summary 不超过 {chars} 字，说明这条到底讲了什么，不要重复标题",
        "3. 只写正文里出现的事实，正文没提的不要编",
        "4. 编号逐条对应，不能遗漏",
        "",
        "条目：",
    ]
    for index, item in enumerate(items):
        lines.append(f'{index}. 标题：{item["title"]}')
        excerpt = excerpts.get(item["id"]) or ""
        if excerpt:
            lines.append(f"   正文摘录：{excerpt}")
        else:
            lines.append("   （正文抓取失败，仅依据标题写，尽量克制）")
    return "\n".join(lines)


def _apply(items: list[dict], rows: list[dict]) -> int:
    by_index: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            by_index[int(row.get("i"))] = row
        except (TypeError, ValueError):
            continue

    written = 0
    for index, item in enumerate(items):
        row = by_index.get(index)
        if not row:
            continue
        summary = str(row.get("summary", "")).strip()
        if summary:
            item["summary"] = summary
            written += 1
    return written


def summarize(items: list[dict], cfg: dict, offline: bool = False) -> tuple[int, str]:
    """返回 (写入摘要条数, 说明)。任何失败都不抛错。"""
    conf = (cfg.get("llm", {}) or {}).get("summary", {}) or {}
    if not conf.get("enabled", True):
        return 0, "摘要阶段已关闭"
    if not items:
        return 0, "没有条目，跳过摘要"

    creds = classify.credentials(cfg)
    if not creds:
        return 0, "没有可用的模型凭据，跳过摘要"

    profile, key, label = creds
    plan = classify.budget(cfg)
    # 离线模式不联网：既保证测试可复现，也避免本地调试时打别人的站
    excerpts = {} if offline else gather(items, conf)
    got = sum(1 for text in excerpts.values() if text)

    prompt = _build_prompt(items, excerpts, plan["summary_chars"])
    attempts = [True, False] if profile.get("json_mode", True) else [False]

    last_error: Exception | None = None
    for json_mode in attempts:
        try:
            content = classify.chat(
                prompt, profile, key, plan["summary_max_tokens"], json_mode
            )
            rows = classify.parse_rows(content)
            written = _apply(items, rows)
            usage = classify.usage_text()
            return written, (
                f"摘要（{label}）：抓到正文 {got}/{len(items)} 条，"
                f"写入 {written} 条"
                + (f"，{usage}" if usage else "")
            )
        except Exception as exc:
            last_error = exc

    return 0, (
        f"摘要失败（{type(last_error).__name__}：{str(last_error)[:160]}），"
        "本期不带摘要"
    )
