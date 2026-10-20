"""各类型源的抓取器：统一返回「原始行」，字段解析交给 merge.prepare。"""

from __future__ import annotations

import datetime as dt
import json
import urllib.parse as up
import xml.etree.ElementTree as ET

from . import http

UTC = dt.timezone.utc
ATOM = {"a": "http://www.w3.org/2005/Atom"}


def fetch(src: dict, start: dt.datetime, end: dt.datetime) -> list[dict]:
    kind = src.get("kind")
    if kind == "hn_algolia":
        return _hn_algolia(src, start, end)
    if kind == "rss":
        return _rss(src)
    raise ValueError(f"未知的源类型: {kind!r}")


def _hn_algolia(src: dict, start: dt.datetime, end: dt.datetime) -> list[dict]:
    """一次查询覆盖整个时间窗，比轮询首页更能捞到高分内容。"""
    query = up.urlencode(
        {
            "tags": "story",
            "numericFilters": (
                f"created_at_i>{int(start.timestamp())},"
                f"created_at_i<{int(end.timestamp())},"
                f"points>{src.get('min_points', 80)}"
            ),
            "hitsPerPage": src.get("limit", 100),
        }
    )
    raw = http.get(
        f"https://hn.algolia.com/api/v1/search?{query}",
        cache_key=f"hn-{end:%Y%m%d%H}",
    )
    rows = []
    for hit in json.loads(raw).get("hits", []):
        sid = hit.get("objectID")
        if not sid:
            continue
        rows.append(
            {
                "id": f"hn:{sid}",
                "source": src["id"],
                "title": (hit.get("title") or "").strip(),
                "url": hit.get("url") or f"https://news.ycombinator.com/item?id={sid}",
                "discussion": f"https://news.ycombinator.com/item?id={sid}",
                "published_at": dt.datetime.fromtimestamp(
                    int(hit.get("created_at_i") or 0), UTC
                ).isoformat(),
                "points": int(hit.get("points") or 0),
                "comments": int(hit.get("num_comments") or 0),
            }
        )
    return rows


def _rss(src: dict) -> list[dict]:
    raw = http.get(src["url"], cache_key=f"rss-{src['id']}", ua=src.get("ua"))
    rows = []
    for title, link, published in _parse_feed(raw):
        if not title or not link:
            continue
        rows.append(
            {
                "id": f"{src['id']}:{link}",
                "source": src["id"],
                "title": title,
                "url": link,
                "discussion": None,
                "published_at": published,
                "points": 0,
                "comments": 0,
            }
        )
    return rows


def _parse_feed(raw: bytes) -> list[tuple[str, str, str]]:
    """同时兼容 RSS 2.0 与 Atom，返回 (标题, 链接, 时间) 三元组。"""
    root = ET.fromstring(raw)

    if root.tag.endswith("}feed") or root.tag == "feed":
        out = []
        for node in root.findall("a:entry", ATOM):
            title = (node.findtext("a:title", default="", namespaces=ATOM) or "").strip()
            link = ""
            for candidate in node.findall("a:link", ATOM):
                if candidate.get("rel", "alternate") == "alternate":
                    link = (candidate.get("href") or "").strip()
                    break
            if not link:
                link = (node.findtext("a:id", default="", namespaces=ATOM) or "").strip()
            published = node.findtext("a:published", default="", namespaces=ATOM) or (
                node.findtext("a:updated", default="", namespaces=ATOM)
            )
            out.append((title, link, (published or "").strip()))
        return out

    out = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not link:
            guid = (node.findtext("guid") or "").strip()
            link = guid if guid.startswith("http") else ""
        out.append((title, link, (node.findtext("pubDate") or "").strip()))
    return out
