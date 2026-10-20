"""LLM 分节：一次调用把候选标题分到固定分节表，顺带生成一句话摘要。

设计原则
--------
* 一次请求搞定全部候选，不做 per-item 调用（成本与延迟都在一个量级内）。
* 只认「OpenAI 兼容端点」这一种协议，端点/模型/密钥变量名全部可配，
  换服务商只改 sources.toml 三行，不动代码。
* 任何失败都不抛错、不阻断构建——分类是增强，不是依赖。
* 输出必须能被严格校验：分节 id 不在表里就丢弃，摘要超长就丢弃。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.request
from zoneinfo import ZoneInfo

MAX_SUMMARY = 80
EXAMPLE = '{"items":[{"i":0,"section":"ai","summary":"模型发布，上下文扩到 1M"}]}'

# 默认指向 DeepSeek。任何 OpenAI 兼容服务（Moonshot / 智谱 / 硅基流动 / 自建 vLLM）
# 都能通过 sources.toml 的 [llm] 覆盖这三个值
DEFAULTS = {
    "endpoint": "https://api.deepseek.com/chat/completions",
    "model": "deepseek-chat",
    "key_env": "DEEPSEEK_API_KEY",
    "timeout": 90,
    "json_mode": True,
}


def _fake_path() -> str | None:
    return os.environ.get("DIGEST_LLM_FAKE") or None


def _profile(cfg: dict) -> dict:
    profile = dict(DEFAULTS)
    profile.update(cfg.get("llm", {}) or {})
    return profile


def resolve(cfg: dict) -> tuple[dict, str] | None:
    """返回 (配置, 密钥)；没有密钥返回 None。"""
    profile = _profile(cfg)
    key = (os.environ.get(str(profile["key_env"])) or "").strip()
    return (profile, key) if key else None


def _parse_span(text: str) -> tuple[int, int]:
    """'08:30-00:30' -> (510, 30)，单位是当天分钟数。"""
    start, end = text.split("-")
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    return sh * 60 + sm, eh * 60 + em


def _in_span(minute: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def budget(cfg: dict, now: dt.datetime | None = None) -> dict:
    """按北京时间决定这轮可以花多少 token。

    DeepSeek 官方模型的错峰优惠是 00:30-08:30（北京时间），其余时段是高峰。
    高峰时段压低输出：少送候选、限制 max_tokens、摘要缩短。
    """
    llm = cfg.get("llm", {}) or {}
    conf = llm.get("budget", {}) or {}
    tz = ZoneInfo(str(llm.get("timezone", "Asia/Shanghai")))
    now = now or dt.datetime.now(tz)
    start, end = _parse_span(str(conf.get("peak", "08:30-00:30")))
    peak = _in_span(now.hour * 60 + now.minute, start, end)

    if peak:
        return {
            "peak": True,
            "limit": int(conf.get("peak_limit", 30)),
            "max_tokens": int(conf.get("peak_max_tokens", 800)),
            "summary_chars": int(conf.get("peak_summary_chars", 20)),
        }
    return {
        "peak": False,
        "limit": int(llm.get("limit", 60)),
        "max_tokens": int(conf.get("offpeak_max_tokens", 4000)),
        "summary_chars": int(conf.get("summary_chars", 30)),
    }


def enabled(cfg: dict) -> bool:
    """没凭据时安静退回关键词分类，不让本地开发和 CI 卡住。"""
    if not (cfg.get("llm", {}) or {}).get("enabled", True):
        return False
    return bool(_fake_path()) or resolve(cfg) is not None


def _build_prompt(items: list[dict], sections: list[dict], summary_chars: int = 30) -> str:
    lines = ["你是技术信息聚合的编辑。把下面每条标题分到唯一的分类，并写一句中文摘要。", ""]
    lines.append("分类表（section 只能取这些 id）：")
    for section in sections:
        lines.append(f'- {section["id"]}：{section["title"]}（{section.get("desc", "")}）')
    lines.append("- other：确实不属于以上任何一类")
    lines += [
        "",
        "要求：",
        f"1. 只输出 JSON，形如 {EXAMPLE}",
        "2. i 是输入编号，必须逐条对应，不能遗漏，也不要编造不存在的编号",
        f"3. summary 用中文，不超过 {summary_chars} 字，只陈述事实，不要评价",
        "4. 拿不准时选最接近的，只有完全无关才用 other",
        "",
        "标题列表：",
    ]
    for index, item in enumerate(items):
        lines.append(f'{index}. {item["title"]}')
    return "\n".join(lines)


def _call(prompt: str, profile: dict, key: str, json_mode: bool, max_tokens: int) -> str:
    fake = _fake_path()
    if fake:
        with open(fake, encoding="utf-8") as handle:
            return handle.read()

    body: dict = {
        "model": profile["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    request = urllib.request.Request(
        profile["endpoint"],
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=int(profile["timeout"])) as resp:
        status = resp.status
        raw = resp.read()

    snippet = raw[:200].decode("utf-8", "replace").replace("\n", " ")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"HTTP {status} 但响应不是 JSON：{snippet}") from None
    if "choices" not in payload:
        raise ValueError(f"响应缺少 choices：{str(payload)[:200]}")
    return payload["choices"][0]["message"]["content"]


def _parse(content: str) -> list[dict]:
    """容忍 ```json 围栏和前后废话，只取第一个完整的 JSON 对象。"""
    text = re.sub(r"```(?:json)?", "", content)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"响应里没有 JSON 对象：{content[:120]!r}")
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
    profile = _profile(cfg)
    resolved = resolve(cfg)

    if resolved:
        key = resolved[1]
        label = str(profile["model"])
    elif _fake_path():
        key, label = "", "fake"
    else:
        return 0, f'没有可用的模型凭据（{profile["key_env"]}），使用关键词分类'

    plan = budget(cfg)
    prompt = _build_prompt(items, sections, plan["summary_chars"])
    # 第一次带 response_format；不支持 JSON 模式的端点会在第二次去掉它重试
    attempts = [True, False] if profile.get("json_mode", True) else [False]

    last_error: Exception | None = None
    for json_mode in attempts:
        try:
            rows = _parse(_call(prompt, profile, key, json_mode, plan["max_tokens"]))
            changed = apply(items, sections, rows)
            phase = "高峰省流" if plan["peak"] else "错峰"
            return changed, (
                f"LLM 分类（{label}，{phase}，max_tokens={plan['max_tokens']}）："
                f"返回 {len(rows)} 条，改判 {changed} 条"
            )
        except Exception as exc:  # 网络、限流、格式，全都退回关键词
            last_error = exc

    return 0, (
        f"LLM 分类失败（{label}，{type(last_error).__name__}：{str(last_error)[:200]}），"
        "退回关键词分类"
    )
