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
        f"你是财经编辑。从下面候选里挑出最值得读者知道的 {want} 条，按重要性排序。",
        "",
        "标准：宏观政策 > 市场异动 > 公司重大事件 > 一般新闻；同题材只留最重要的一条。",
        "",
        "要求：",
        f'1. 只输出 JSON，形如 {{"picks":[3,0,7]}}，按重要性从高到低，最多 {want} 条',
        "2. 数字是候选编号，必须真实存在，不要编造",
        f"3. 目标是 {want} 条；候选不足就全部选出。只有明显无关的（娱乐八卦、生活琐事）才剔除",
        "4. 尽量覆盖不同来源，同一来源别超过半数；官方机构（美联储/欧洲央行/SEC/BLS）优先",
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
            chosen = _ensure_diversity(chosen[:want], candidates, want)
            return chosen, (
                f"模型选品（{label}）：{len(candidates)} 条候选挑 {len(chosen)} 条，"
                f"来源 {len({x['source'] for x in chosen})} 个"
            )
        except Exception as exc:  # noqa: BLE001 - 选品失败不能拖垮发布
            last = exc

    return _recent(items, want), f"模型选品失败（{type(last).__name__}），按时间取最新"


def _ensure_diversity(chosen: list[dict], candidates: list[dict], want: int) -> list[dict]:
    """每个有候选的来源至少保留一条。

    模型会习惯性地把名额给单一来源（实测 12 条全给 CNBC，把唯一的 ECB 挤掉了），
    光靠 prompt 约束不可靠，这里做确定性兜底。挤掉名额时只动"该来源还有别的代表"的条目，
    避免拆东墙补西墙。
    """
    by_source: dict[str, list[dict]] = {}
    for item in candidates:
        by_source.setdefault(item["source"], []).append(item)

    result = list(chosen)
    covered = {item["source"] for item in result}
    missing = [src for src in by_source if src not in covered]

    for src in missing:
        best = max(by_source[src], key=lambda x: x["published"])
        if len(result) >= want:
            # 从尾部找一条「所在来源还有别的代表」的，腾出位置
            for index in range(len(result) - 1, -1, -1):
                source = result[index]["source"]
                if sum(1 for x in result if x["source"] == source) > 1:
                    result.pop(index)
                    break
            else:
                break  # 实在腾不出来，就放弃这条
        result.append(best)
    return result


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
