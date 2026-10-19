"""URL 归一化与标题相似度：去重的地基。"""

from __future__ import annotations

import re
import urllib.parse as up

TRACKING = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "spm",
    "from", "share", "share_token",
}

CJK = re.compile(r"[\u4e00-\u9fff]")


def canonical_url(url: str) -> str:
    """把同一个页面可能出现的各种写法收敛成一个字符串。"""
    if not url:
        return ""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parts = up.urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "youtu.be":
        host, parts_path = "youtube.com", parts.path.strip("/")
        parts = parts._replace(path="/watch", query=f"v={parts_path}")

    path = parts.path.rstrip("/") or "/"
    query = [
        (k, v)
        for k, v in up.parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING
    ]
    if host == "youtube.com" and path == "/watch":
        vid = dict(query).get("v")
        query = [("v", vid)] if vid else []

    suffix = f"?{up.urlencode(query)}" if query else ""
    return f"{host}{path}{suffix}"


def title_tokens(title: str) -> set[str]:
    """中文用二元字符，英文用词，够做粗粒度相似度。"""
    text = (title or "").lower()
    if CJK.search(text):
        text = re.sub(r"[^\u4e00-\u9fffa-z0-9]+", "", text)
        return {text[i : i + 2] for i in range(len(text) - 1)}
    return {w for w in re.findall(r"[a-z0-9]{2,}", text)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def host_of(url: str) -> str:
    try:
        host = (up.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host
