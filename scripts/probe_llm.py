"""本地探测 LLM provider：复现「分类 400 / 摘要成功」这种不对称失败。

用法：
    OPENROUTER_API_KEY=sk-or-... python3 scripts/probe_llm.py
    DEEPSEEK_API_KEY=sk-...     python3 scripts/probe_llm.py --provider deepseek

依次发 4 个请求，逐步排除变量：
    1. 最简请求（不带 response_format、不带 reasoning）
    2. 只带 reasoning 开关
    3. 只带 response_format（JSON 模式）
    4. 和生产分类请求等价（response_format + reasoning + max_tokens=800）
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import classify, config  # noqa: E402

PROMPT = '把下面三条分到 ai / other 两类，只输出 JSON：{"items":[{"i":0,"section":"ai"}]}\n1. A new LLM released\n2. A database benchmark\n3. A cooking video'


def call(plan: dict, key: str, *, json_mode: bool, reasoning: bool, max_tokens: int) -> tuple:
    body = {
        "model": plan["model"],
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if not reasoning:
        body["reasoning"] = {"enabled": False}
    if plan.get("thinking") not in (None, "", "auto"):
        body["thinking"] = {"type": plan["thinking"]}

    request = urllib.request.Request(
        plan["endpoint"],
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            payload = json.loads(resp.read())
        choice = payload["choices"][0]
        message = choice.get("message", {}) or {}
        content = (message.get("content") or "").strip()
        return (
            "ok",
            f"finish={choice.get('finish_reason')} content={content[:80]!r} "
            f"usage={payload.get('usage')}",
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace").replace("\n", " ")
        return "http_error", f"{exc.code} {detail}"
    except Exception as exc:  # noqa: BLE001
        return "error", f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default=None, help="openrouter / deepseek，默认取配置")
    args = parser.parse_args()

    cfg = config.load("tech")
    if args.provider:
        cfg.setdefault("llm", {})["provider"] = args.provider
    plan = classify.profile(cfg)
    key = (os.environ.get(str(plan["key_env"])) or "").strip()
    print(f"provider={plan['provider']} model={plan['model']} key_env={plan['key_env']}")
    if not key:
        print(f"缺少 {plan['key_env']}，无法探测")
        return 1

    cases = [
        ("1 最简（无 json/reasoning）", dict(json_mode=False, reasoning=True, max_tokens=200)),
        ("2 关 reasoning", dict(json_mode=False, reasoning=False, max_tokens=200)),
        ("3 只开 JSON 模式", dict(json_mode=True, reasoning=True, max_tokens=200)),
        ("4 生产等价（json+关推理+800）", dict(json_mode=True, reasoning=False, max_tokens=800)),
    ]
    for title, kwargs in cases:
        status, detail = call(plan, key, **kwargs)
        print(f"\n{title}\n  {status}: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
