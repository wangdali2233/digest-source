"""选品策略：栏目用 rank 字段声明用哪一种。

* ``percentile``：有分数信号（HN points）时用，源内百分位 + 源权重。
* ``llm``：没有分数信号时用（财经），把候选交给模型挑，失败退回按时间取最新的。
"""

from __future__ import annotations

import json

from . import classify, merge


def select(cfg: dict, items: list[dict], sections_cfg: list[dict], quota: dict) -> tuple[list[dict], str]:
    """返回 (入选条目, 说明)。"""
    strategy = str((cfg.get("profile") or {}).get("rank", "percentile"))

    if strategy == "llm":
        return _llm(cfg, items, quota)

    weights = {src["id"]: float(src.get("weight", 0.7)) for src in cfg.get("sources", [])}
    scored = merge.score(items, weights)
    return merge.select(scored, sections_cfg, quota), "按分数选品"


def _llm(cfg: dict, items: list[dict], quota: dict) -> tuple[list[dict], str]:
    want = int(quota.get("daily", 6))
    creds = classify.credentials(cfg)
    if not creds:
        return _recent(items, want), "没有模型凭据，按时间取最新"

    profile, key, label = creds
    plan = classify.budget(cfg)
    candidates = sorted(items, key=lambda x: x["published"], reverse=True)[: plan["limit"]]

    lines = [
        "你是财经编辑。从下面候选里挑出最值得读者知道的几条，按重要性排序。",
        "",
        "标准：宏观政策 > 市场异动 > 公司重大事件 > 一般新闻；同题材只留最重要的一条。",
        "",
        "要求：",
        f'1. 只输出 JSON，形如 {{"picks":[3,0,7]}}，最多 {want} 条',
        "2. 数字是候选编号，必须真实存在，不要编造",
        "3. 不够重要就少挑，宁缺毋滥",
        "",
        "候选：",
    ]
    for index, item in enumerate(candidates):
        lines.append(f'{index}. [{item.get("source_name", "")}] {item["title"]}')

    attempts = [True, False] if profile.get("json_mode", True) else [False]
    for json_mode in attempts:
        try:
            content = classify.chat("\n".join(lines), profile, key, 600, json_mode)
            picks = _parse_picks(content)
            chosen = [candidates[i] for i in picks if 0 <= i < len(candidates)]
            if not chosen:
                raise ValueError("模型没有给出有效编号")
            return chosen[:want], f"模型选品（{label}）：{len(candidates)} 条候选挑 {len(chosen)} 条"
        except Exception as exc:  # noqa: BLE001 - 选品失败不能拖垮发布
            last = exc

    return _recent(items, want), f"模型选品失败（{type(last).__name__}），按时间取最新"


def _recent(items: list[dict], want: int) -> list[dict]:
    return sorted(items, key=lambda x: x["published"], reverse=True)[:want]


def _parse_picks(content: str) -> list[int]:
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"响应里没有 JSON：{content[:80]!r}")
    data = json.loads(text[start : end + 1])
    raw = data.get("picks") or data.get("items") or []
    picks: list[int] = []
    for value in raw:
        if isinstance(value, dict):
            value = value.get("i")
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if index not in picks:
            picks.append(index)
    return picks
