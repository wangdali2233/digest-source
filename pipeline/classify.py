"""LLM 分节：一次调用把候选标题分到固定分节表，顺带生成一句话摘要。

设计原则
--------
* 一次请求搞定全部候选，不做 per-item 调用（成本与延迟都在一个量级内）。
* 任何失败都退回关键词分类，不抛错、不阻断构建——分类是增强，不是依赖。
* 输出必须能被严格校验：分节 id 不在表里就丢弃，摘要超长就丢弃。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

DEFAULT_ENDPOINT = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
MAX_SUMMARY = 80

EXAMPLE = '{"items":[{"i":0,"section":"ai","summary":"模型发布，上下文扩到 1M"}]}'


def enabled(cfg: dict) -> bool:
    """没配 key 时安静退回关键词分类，不让本地开发和 CI 卡住。"""
    llm = cfg.get("llm", {})
    if not llm.get("enabled", True):
        return False
    return bool(os.environ.get("DEEPSEEK_API_KEY")) or bool(os.environ.get("DIGEST_LLM_FAKE"))


def _build_prompt(items: list[dict], sections: list[dict]) -> str:
    lines = ["你是技术信息聚合的编辑。把下面每条标题分到唯一的分类，并写一句中文摘要。", ""]
    lines.append("分类表（section 只能取这些 id）：")
    for section in sections:
        desc = section.get("desc", "")
        lines.append(f'- {section["id"]}：{section["title"]}（{desc}）')
    lines.append("- other：确实不属于以上任何一类")
    lines += [
        "",
        "要求：",
        f"1. 只输出 JSON，形如 {EXAMPLE}",
        "2. i 是输入编号，必须逐条对应，不能遗漏，也不要编造不存在的编号",
        "3. summary 用中文，不超过 30 字，只陈述事实，不要评价",
        "4. 拿不准时选最接近的，只有完全无关才用 other",
        "",
        "标题列表：",
    ]
    for index, item in enumerate(items):
        lines.append(f'{index}. {item["title"]}')
    return "\n".join(lines)


def _call(prompt: str, llm: dict) -> str:
    fake = os.environ.get("DIGEST_LLM_FAKE")
    if fake:
        return open(fake, encoding="utf-8").read()

    body = {
        "model": llm.get("model", DEFAULT_MODEL),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    if llm.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}

    request = urllib.request.Request(
        llm.get("endpoint", DEFAULT_ENDPOINT),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}",
        },
    )
    with urllib.request.urlopen(request, timeout=int(llm.get("timeout", 60))) as resp:
        payload = json.loads(resp.read())
    return payload["choices"][0]["message"]["content"]


def _parse(content: str) -> list[dict]:
    """容忍 ```json 围栏和前后废话，只取第一个完整的 JSON 对象。"""
    text = re.sub(r"```(?:json)?", "", content)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应里没有 JSON 对象")
    data = json.loads(text[start : end + 1])
    rows = data.get("items") or data.get("results") or []
    if not isinstance(rows, list):
        raise ValueError("items 不是数组")
    return rows


def apply(items: list[dict], sections: list[dict], rows: list[dict]) -> int:
    """把模型结果写回 items，返回改判的条数。越界编号和非法分节一律丢弃。"""
    valid = {section["id"] for section in sections} | {"other"}
    by_index: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            by_index[int(row.get("i"))] = row
        except (TypeError, ValueError):
            continue

    changed = 0
    for index, item in enumerate(items):
        row = by_index.get(index)
        if not row:
            continue
        section = str(row.get("section", "")).strip().lower()
        if section in valid and section != item["section"]:
            item["section"] = section
            changed += 1
        summary = str(row.get("summary", "")).strip()
        if summary and len(summary) <= MAX_SUMMARY:
            item["summary"] = summary
    return changed


def classify(items: list[dict], sections: list[dict], cfg: dict) -> tuple[int, str]:
    """返回 (改判条数, 说明)。异常一律吞掉并回退。"""
    llm = dict(cfg.get("llm", {}))
    prompt = _build_prompt(items, sections)

    last_error: Exception | None = None
    for _ in range(2):
        try:
            rows = _parse(_call(prompt, llm))
            changed = apply(items, sections, rows)
            return changed, f"LLM 分类：返回 {len(rows)} 条，改判 {changed} 条"
        except Exception as exc:  # 网络、限流、格式，全都退回关键词
            last_error = exc

    return 0, f"LLM 分类失败（{type(last_error).__name__}），退回关键词分类"
