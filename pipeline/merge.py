"""归一化、去重、打分、选品。

顺序很重要：先去重再打分，否则同一篇文章的多个来源会互相抬分。
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from email.utils import parsedate_to_datetime

from .normalize import canonical_url, host_of, jaccard, title_tokens

UTC = dt.timezone.utc
OTHER = "other"


def parse_datetime(value: str | None) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def classify(
    title: str, host: str, default: str | None, sections_cfg: list[dict]
) -> str:
    """标题关键词 + 域名双重判断，都没有命中才回落到源的默认分节。

    域名比标题更可靠（arxiv.org 一定是论文），所以权重给 2 而不是 1。
    """
    text = (title or "").lower()
    best, best_hits = None, 0
    for section in sections_cfg:
        hits = sum(1 for kw in section.get("keywords", []) if kw.lower() in text)
        if host and host in {d.lower() for d in section.get("domains", [])}:
            hits += 2
        if hits > best_hits:
            best, best_hits = section["id"], hits
    return best or default or OTHER


def prepare(
    rows: list[dict],
    sections_cfg: list[dict],
    start: dt.datetime,
    end: dt.datetime,
    sources_by_id: dict,
) -> list[dict]:
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        url = (row.get("url") or "").strip()
        if not title or not url:
            continue
        published = parse_datetime(row.get("published_at"))
        if published is None or not (start <= published <= end):
            continue
        src = sources_by_id.get(row.get("source"), {})
        host = host_of(url)
        items.append(
            {
                "id": row.get("id") or url,
                "source": row.get("source") or "unknown",
                "source_name": src.get("name", row.get("source") or ""),
                "title": title,
                "url": url,
                "canonical": canonical_url(url),
                "discussion": row.get("discussion"),
                "published": published,
                "points": int(row.get("points") or 0),
                "comments": int(row.get("comments") or 0),
                "host": host,
                "section": classify(title, host, src.get("default_section"), sections_cfg),
                "age_hours": max(0.0, (end - published).total_seconds() / 3600),
            }
        )
    return items


def dedupe(items: list[dict], threshold: float = 0.6, hours: float = 48.0) -> list[dict]:
    """两层去重：同一 URL 精确合并，同一事件按标题相似度合并。"""
    exact: dict[str, dict] = {}
    for item in items:
        cur = exact.get(item["canonical"])
        if cur is None or item["points"] > cur["points"]:
            exact[item["canonical"]] = item

    ordered = sorted(exact.values(), key=lambda x: (-x["points"], x["title"]))
    kept: list[dict] = []
    for item in ordered:
        tokens = title_tokens(item["title"])
        for head in kept:
            gap = abs((item["published"] - head["published"]).total_seconds())
            if gap > hours * 3600:
                continue
            if jaccard(tokens, title_tokens(head["title"])) >= threshold:
                head.setdefault("see_also", []).append(
                    {"title": item["title"], "url": item["url"]}
                )
                break
        else:
            kept.append(item)
    return kept


def score(items: list[dict], weights: dict[str, float]) -> list[dict]:
    """源内先做百分位归一，再乘源权重，跨源分数才有可比性。"""
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        groups[item["source"]].append(item)

    for group in groups.values():
        points = sorted(item["points"] for item in group)
        n = len(points)
        for item in group:
            if not item["points"] or n == 1:
                percentile = 0.55 if not item["points"] else 0.8
            else:
                rank = bisect.bisect_right(points, item["points"]) - 1
                percentile = rank / (n - 1)
            decay = max(0.7, 1.0 - 0.3 * item["age_hours"] / 24)
            item["score"] = round(
                percentile * weights.get(item["source"], 0.7) * decay, 4
            )
    return items


def select(items: list[dict], sections_cfg: list[dict], quota: dict) -> list[dict]:
    """按分节配额挑选，保证版面多样性；最后受总量约束。"""
    by_section: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_section[item["section"]].append(item)

    order = [section["id"] for section in sections_cfg] + [OTHER]
    # 「其他」是垃圾桶，必须限量，否则高分但没分好类的条目会淹没版面
    other_cap = int(quota.get("max_other", 2))
    picked: list[dict] = []
    for section_id in order:
        cap = other_cap if section_id == OTHER else int(quota["per_section"])
        group = sorted(by_section.get(section_id, []), key=lambda x: -x["score"])
        picked.extend(group[:cap])

    picked.sort(key=lambda x: -x["score"])
    return picked[: int(quota["daily"])]
