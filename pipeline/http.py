"""最小 HTTP 客户端：带 ETag / Last-Modified 条件请求，只用标准库。

缓存的目的不是省流量，而是让重复运行（本地调试、CI 重试）不会因为
源站的限流而失败；命中 304 时直接返回上次的 body。
"""

from __future__ import annotations

import json
import pathlib
import urllib.error
import urllib.request

from .config import ROOT

CACHE_DIR = ROOT / "data" / "cache"
UA = "digest-bot/1.0 (+https://github.com/wangdali2233/digest-source)"


def _cache_path(key: str) -> pathlib.Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)[:120]
    return CACHE_DIR / f"{safe}.json"


def get(url: str, cache_key: str | None = None, timeout: int = 25, ua: str | None = None) -> bytes:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    cached: dict = {}
    if cache_key:
        path = _cache_path(cache_key)
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = {}

    headers = {"User-Agent": ua or UA, "Accept-Encoding": "identity"}
    if cached.get("etag"):
        headers["If-None-Match"] = cached["etag"]
    if cached.get("modified"):
        headers["If-Modified-Since"] = cached["modified"]

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            if cache_key:
                _cache_path(cache_key).write_text(
                    json.dumps(
                        {
                            "etag": resp.headers.get("ETag"),
                            "modified": resp.headers.get("Last-Modified"),
                            "body": body.decode("utf-8", "replace"),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            return body
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached.get("body"):
            return cached["body"].encode("utf-8")
        raise
