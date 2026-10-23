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
# 最近一次调用的 usage，用来把 token 消耗打进口志（成本可观测）
LAST_USAGE: dict = {}
EXAMPLE = '{"items":[{"i":0,"section":"ai","tags":["ai","llm"],"summary":"模型发布，上下文扩到 1M"}]}'

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


def _profile_for(cfg: dict, name: str) -> dict:
    """全局 [llm] 的标量设置 + [llm.<provider>] 的覆盖。嵌套表（budget/summary）不参与合并。"""
    llm = cfg.get("llm", {}) or {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in llm.items() if not isinstance(v, dict)})
    block = llm.get(name) or {}
    if isinstance(block, dict):
        merged.update({k: v for k, v in block.items() if not isinstance(v, dict)})
    merged["provider"] = name
    return merged


def profile(cfg: dict) -> dict:
    """当前默认 provider 的有效配置。"""
    llm = cfg.get("llm", {}) or {}
    return _profile_for(cfg, str(llm.get("provider", "deepseek")).lower())


PROVIDERS = ("openrouter", "deepseek")


def resolve(cfg: dict) -> tuple[dict, str] | None:
    """返回 (配置, 密钥)。先试默认 provider，没密钥再试另一个。"""
    llm = cfg.get("llm", {}) or {}
    preferred = str(llm.get("provider", "deepseek")).lower()
    order = [preferred] + [name for name in PROVIDERS if name != preferred]

    for name in order:
        plan = _profile_for(cfg, name)
        key = (os.environ.get(str(plan["key_env"])) or "").strip()
        if key:
            return plan, key
    return None


def _parse_span(text: str) -> tuple[int, int]:
    """'08:30-00:30' -> (510, 30)，单位是当天分钟数。"""
    start, end = text.split("-")
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    return sh * 60 + sm, eh * 60 + em


def _spans(value) -> list[tuple[int, int]]:
    """接受 "09:00-12:00" 或 ["09:00-12:00", "14:00-18:00"]。"""
    items = value if isinstance(value, (list, tuple)) else [value]
    return [_parse_span(str(item)) for item in items]


def _in_span(minute: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def budget(cfg: dict, now: dt.datetime | None = None) -> dict:
    """按北京时间决定这轮可以花多少 token。

    高峰时段（北京时间 09:00-12:00、14:00-18:00，可配置多个区间）压低输出：
    少送候选、限制 max_tokens、摘要缩短。其余时段视为错峰，放开预算。
    """
    llm = cfg.get("llm", {}) or {}
    conf = llm.get("budget", {}) or {}
    tz = ZoneInfo(str(llm.get("timezone", "Asia/Shanghai")))
    now = now or dt.datetime.now(tz)
    minute = now.hour * 60 + now.minute
    peak = any(_in_span(minute, s, e) for s, e in _spans(conf.get("peak", ["09:00-12:00", "14:00-18:00"])))

    if peak:
        return {
            "peak": True,
            "limit": int(conf.get("peak_limit", 30)),
            "max_tokens": int(conf.get("peak_max_tokens", 800)),
            "summary_chars": int(conf.get("peak_summary_chars", 20)),
            "summary_max_tokens": int(conf.get("peak_summary_max_tokens", 1600)),
        }
    return {
        "peak": False,
        "limit": int(llm.get("limit", 60)),
        "max_tokens": int(conf.get("offpeak_max_tokens", 4000)),
        "summary_chars": int(conf.get("summary_chars", 30)),
        "summary_max_tokens": int(conf.get("offpeak_summary_max_tokens", 4000)),
    }


def credentials(cfg: dict) -> tuple[dict, str, str] | None:
    """返回 (profile, key, label)；fake 模式也算「有凭据」，否则离线测试跑不通摘要阶段。"""
    resolved = resolve(cfg)
    if resolved:
        plan, key = resolved
        return plan, key, f'{plan["provider"]}:{plan["model"]}'
    if _fake_path():
        plan = profile(cfg)
        return plan, "", "fake"
    return None


def enabled(cfg: dict) -> bool:
    """没凭据时安静退回关键词分类，不让本地开发和 CI 卡住。"""
    if not (cfg.get("llm", {}) or {}).get("enabled", True):
        return False
    return bool(_fake_path()) or resolve(cfg) is not None


def _build_prompt(
    items: list[dict],
    sections: list[dict],
    summary_chars: int = 35,
    vocab: list[str] | None = None,
) -> str:
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
    ]
    if vocab:
        lines.append("5. tags：从下面的受控词表里选 1-3 个最贴切的，**只能选词表里的**，不确定就少选")
        lines.append("   词表：" + ", ".join(vocab))
    lines += ["", "标题列表："]
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
    # 分类任务不需要推理：开着思考会把 max_tokens 全烧在 reasoning 上，
    # 正文直接返回空字符串（实测 deepseek-flash + 高峰 800 token 就是这样）。
    thinking = str(profile.get("thinking", "disabled"))
    if thinking and thinking != "auto":
        body["thinking"] = {"type": thinking}

    # OpenRouter 的推理开关叫 reasoning。免费路由默认会开推理，
    # 把 max_tokens 全烧在 reasoning 上，正文返回空（finish_reason=length）。
    reasoning = profile.get("reasoning")
    if reasoning is False:
        body["reasoning"] = {"enabled": False}
    elif isinstance(reasoning, dict):
        body["reasoning"] = reasoning

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

    LAST_USAGE.clear()
    LAST_USAGE.update(payload.get("usage") or {})

    choice = payload["choices"][0]
    message = choice.get("message", {}) or {}
    content = (message.get("content") or "").strip()
    if not content:
        detail = {
            "finish_reason": choice.get("finish_reason"),
            "fields": [k for k in message if k != "content"],
            "usage": payload.get("usage"),
        }
        raise ValueError(f"content 为空，诊断：{json.dumps(detail, ensure_ascii=False)[:300]}")
    return content


def usage_text() -> str:
    if not LAST_USAGE:
        return ""
    return f"{LAST_USAGE.get('prompt_tokens', 0)}/{LAST_USAGE.get('completion_tokens', 0)} tokens"


def chat(prompt: str, profile: dict, key: str, max_tokens: int, json_mode: bool = True) -> str:
    """给摘要阶段复用的底层调用。"""
    return _call(prompt, profile, key, json_mode, max_tokens)


def parse_rows(content: str) -> list[dict]:
    return _parse(content)


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


def apply(
    items: list[dict],
    sections: list[dict],
    rows: list[dict],
    vocab: list[str] | None = None,
    max_per_item: int = 3,
) -> int:
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

    allowed = {str(t).strip().lower() for t in (vocab or [])}
    changed = 0
    for index, item in enumerate(items):
        row = by_index.get(index)
        if not row:
            continue
        raw_tags = row.get("tags")
        if isinstance(raw_tags, list):
            picked: list[str] = []
            for raw in raw_tags:
                tag = str(raw).strip().lower()
                if tag and tag not in picked and (not allowed or tag in allowed):
                    picked.append(tag)
                if len(picked) >= max_per_item:
                    break
            if picked:
                item["tags"] = picked
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
    creds = credentials(cfg)
    if not creds:
        return 0, f'没有可用的模型凭据（{profile(cfg)["key_env"]}），使用关键词分类'
    plan, key, label = creds

    tokens = budget(cfg)
    tags_cfg = cfg.get("tags", {}) or {}
    vocab = [str(t) for t in tags_cfg.get("vocab", [])]
    prompt = _build_prompt(items, sections, tokens["summary_chars"], vocab)
    # 第一次带 response_format；不支持 JSON 模式的端点会在第二次去掉它重试
    attempts = [True, False] if plan.get("json_mode", True) else [False]

    last_error: Exception | None = None
    for json_mode in attempts:
        try:
            rows = _parse(_call(prompt, plan, key, json_mode, tokens["max_tokens"]))
            changed = apply(items, sections, rows, vocab, int(tags_cfg.get("max_per_item", 3)))
            phase = "高峰省流" if tokens["peak"] else "错峰"
            usage = usage_text()
            return changed, (
                f"LLM 分类（{label}，{phase}，max_tokens={tokens['max_tokens']}）："
                f"返回 {len(rows)} 条，改判 {changed} 条"
                + (f"，{usage}" if usage else "")
            )
        except Exception as exc:  # 网络、限流、格式，全都退回关键词
            last_error = exc

    return 0, (
        f"LLM 分类失败（{label}，{type(last_error).__name__}：{str(last_error)[:200]}），"
        "退回关键词分类"
    )
