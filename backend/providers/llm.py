"""LLM provider for OpenAI-compatible endpoints (DeepSeek / Qwen / OpenAI / vLLM).

Design rules carried over from the technical design:
- LLM generates candidates only; it never decides truth.
- Structured output is requested via JSON response format and validated locally.
- Extraction prompts demand verbatim quotes; verify.py enforces this later.
- Planning and writing use a slightly higher temperature; extraction uses 0.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from backend.config import settings
from backend.core.models import Document, Evidence, SearchHit, SubQuestion

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)

# The planner is an LLM and will drift off the canonical dimension names unless
# they are mapped back. Measured: it answered "市场规模" / "风险监管" / "未来机会"
# while core/writer.py expects "市场分析" / "风险分析" / "商业机会", so grouping in
# the writer found only 3 of 7 sections and silently dropped the rest. Aliases are
# ordered: the first section whose alias appears wins.
_SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "行业背景": ("背景", "政策", "发展历程", "演进"),
    "市场分析": ("市场规模", "市场", "规模", "增长"),
    "竞争格局": ("竞争", "格局", "份额", "主要企业"),
    "技术路线": ("技术", "架构", "路线"),
    "商业模式": ("商业模式", "盈利", "定价", "收费"),
    "商业机会": ("机会", "融资", "资本", "前景"),
    "风险分析": ("风险", "监管", "合规", "挑战"),
}


def _normalize_section(name: str) -> str:
    """Map a free-form section label onto the canonical dimension list."""
    from backend.providers.planner import DIMENSIONS

    canonical = [d[0] for d in DIMENSIONS]
    text = (name or "").strip()
    if text in canonical:
        return text
    for section in canonical:
        for word in _SECTION_ALIASES.get(section, ()):
            if word in text:
                return section
    return text or "其他"



class LLMProvider:
    """Thin OpenAI-compatible chat client with JSON mode and retry."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.llm_api_key
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.model = model or settings.llm_model
        if not self.api_key:
            raise RuntimeError("LLM API key missing; set RESEARCHMIND_LLM_API_KEY")

    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        json_mode: bool = True,
        retries: int = 2,
        reject_truncated: bool = False,
    ) -> str:
        budget = max_tokens or settings.llm_max_tokens
        # Reasoning models (deepseek-v4.1-flash measured here) spend most of the
        # budget on `reasoning_content` before emitting a single content token.
        # With a small ceiling the reply comes back finish_reason="length" and
        # content="" - which downstream reads as "the model found nothing", a
        # silent zero-evidence run. One escalation with a doubled budget fixes
        # the common case without letting a runaway model loop forever.
        attempts: list[int] = [budget, min(budget * 2, 16000)]

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": attempts[min(attempt, len(attempts) - 1)],
                "stream": False,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=settings.llm_timeout_s) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                choice = body["choices"][0]
                content = choice["message"].get("content") or ""
                reason = choice.get("finish_reason")
                # A non-empty but cut-off reply is the most expensive failure
                # mode: it looks like a finished document. Measured on the
                # writer, a 127-row ledger exhausted 8000 tokens mid-sentence in
                # section 6.5 and the run still reported success. Callers that
                # need whole documents opt in to rejecting it so the doubled
                # retry budget below gets a chance to finish the text.
                if content.strip() and not (reject_truncated and reason == "length"):
                    return content
                if content.strip():
                    last_error = RuntimeError(
                        f"LLM output truncated (finish_reason={reason}, "
                        f"max_tokens={payload['max_tokens']}, chars={len(content)})"
                    )
                else:
                    # Empty content is only acceptable when the model genuinely
                    # finished; a truncated reasoning-only reply must be retried.
                    last_error = RuntimeError(
                        f"LLM returned empty content (finish_reason={reason}, "
                        f"max_tokens={payload['max_tokens']})"
                    )
            except (urllib.error.URLError, json.JSONDecodeError, KeyError,
                    IndexError, TimeoutError) as exc:
                last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"LLM call failed: {last_error}") from last_error

    def chat_json(self, system: str, user: str, **kwargs: Any) -> Any:
        raw = self.chat(system, user, json_mode=True, **kwargs)
        cleaned = _JSON_FENCE_RE.sub("", raw.strip())
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start >= 0 and end > start:
                return json.loads(cleaned[start : end + 1])
            raise RuntimeError(f"LLM returned non-JSON: {raw[:200]!r}")

    # -- structured tasks ----------------------------------------------------
    def plan(self, query: str) -> list[SubQuestion]:
        system = (
            "你是一位资深行业研究员。把用户的深度研究问题拆解为 4-6 个互不重叠的子问题。"
            "section 字段只能从这七个值中选，逐字照抄，不得改写："
            "行业背景、市场分析、竞争格局、技术路线、商业模式、商业机会、风险分析。"
            "每个子问题给出 1-3 个可执行的搜索查询，中文题目优先包含中文关键词。"
            "只返回 JSON，格式为 {\"sub_questions\":[{\"id\":\"Q1\",\"section\":\"市场分析\","
            "\"question\":\"...\",\"queries\":[\"...\"]}]}。"
        )
        data = self.chat_json(system, query, temperature=0.2, max_tokens=2000)
        out: list[SubQuestion] = []
        for i, item in enumerate(data.get("sub_questions", [])):
            out.append(SubQuestion(
                id=str(item.get("id") or f"Q{i + 1}"),
                section=_normalize_section(str(item.get("section") or "")),
                question=str(item.get("question") or ""),
                queries=[str(q) for q in item.get("queries", [])][:3],
            ))
        return out

    def extract(self, doc: Document, subq: SubQuestion) -> list[Evidence]:
        system = (
            "你是事实抽取器。只从给定原文中抽取能回答子问题的原子事实。"
            "每条事实必须包含 quote：原文中逐字连续的一段（不要改写、不要翻译、不要拼接）。"
            "若原文没有相关信息，返回空数组。数值必须同时给出 numeric_value 与 numeric_unit。"
            "只返回 JSON：{\"evidence\":[{\"claim\":\"...\",\"quote\":\"...\",\"topic\":\"...\","
            "\"numeric_value\":320,\"numeric_unit\":\"亿元\",\"confidence\":0.9}]}"
        )
        user = (
            f"子问题：{subq.question}\n章节：{subq.section}\n\n"
            f"来源标题：{doc.title}\n来源 URL：{doc.url}\n发布日期：{doc.published_at or '未知'}\n"
            f"来源类型：{doc.source_type}\n\n原文：\n{doc.raw_text[:8000]}"
        )
        data = self.chat_json(system, user, temperature=0.0, max_tokens=3000)
        out: list[Evidence] = []
        for item in data.get("evidence", []):
            quote = str(item.get("quote") or "").strip()
            if not quote:
                continue
            raw_value = item.get("numeric_value")
            try:
                value = float(raw_value) if raw_value not in (None, "") else None
            except (TypeError, ValueError):
                value = None
            out.append(Evidence(
                id="",
                sub_question_id=subq.id,
                section=subq.section,
                claim=str(item.get("claim") or quote[:60]),
                quote=quote,
                source_url=doc.url,
                source_title=doc.title,
                published_at=doc.published_at,
                source_type=doc.source_type,
                topic=str(item.get("topic") or ""),
                numeric_value=value,
                numeric_unit=str(item.get("numeric_unit") or ""),
                confidence=float(item.get("confidence") or 0.9),
            ))
        return out

    def write(self, state: Any) -> str:
        """Render the report one section per call.

        Measured failure this replaces: a single 8000-token call covering the
        whole document. With the ledger at 60 rows it just fit; at 127 rows the
        reply hit the ceiling mid-sentence in section 6.5, sections 6.6-6.8 and
        the reference list disappeared, and the run still reported
        completed_with_conflicts because truncated content is non-empty. Split
        per section, every response stays far below the ceiling, and the
        reference list is rendered deterministically instead of spending tokens
        on text the ledger already knows.
        """
        from backend.core.writer import SECTION_ORDER, SECTION_TITLES, render_sources

        grouped: dict[str, list[Any]] = {}
        for ev in state.evidence:
            grouped.setdefault(ev.section, []).append(ev)

        conflicts_txt = ""
        if state.conflicts:
            rows = [
                f"- {c.topic}: {c.left.numeric_value}{c.unit} [{c.left.id}] vs "
                f"{c.right.numeric_value}{c.unit} [{c.right.id}]"
                for c in state.conflicts
            ]
            conflicts_txt = "\n## 已知数据冲突（正文必须并列呈现两种口径）\n" + "\n".join(rows)

        system = (
            "你是一位专业行业研究员，擅长撰写严谨、可追溯的深度研究报告。"
            "严格遵守：只使用给定证据账本中的事实；每条事实性陈述标注 [E#]；"
            "对冲突数据同时呈现双方口径并说明差异；结构完整、语言精炼、不空泛。"
        )

        def ledger(evs: list[Any]) -> str:
            return "\n".join(
                f"- [{ev.id}] ({ev.section}) {ev.claim}｜原文引述：{ev.quote}｜"
                f"来源：{ev.source_title} {ev.source_url}｜日期：{ev.published_at or '未知'}"
                for ev in evs
            )

        parts: list[str] = [
            "# 深度研究报告",
            "",
            "> 每条事实性陈述后标注 [E#] 引用标记，ID 均来自证据账本；"
            "数字冲突已并列呈现两种口径，未取平均。",
            "",
        ]

        # 摘要和各章互不依赖。串行时实测「1 摘要 + 4-5 章」要 176s，是整条
        # 流水线最长的一段（两轮抽取加起来才 144s）。放进同一个池子里并发，
        # 总时长由最慢的一次调用决定，而不是它们的和。
        def render_summary() -> str:
            try:
                return self.chat(
                    system,
                    "证据账本：\n" + ledger(state.evidence) + conflicts_txt
                    + "\n\n请输出 300-500 字的「执行摘要」正文：不要输出 ## 标题，"
                    "不要列参考资料，直接给段落。",
                    temperature=0.3,
                    max_tokens=1200,
                    json_mode=False,
                    reject_truncated=True,
                ).strip()
            except Exception:
                # 摘要失败不应毁掉整篇报告；正文仍然可用且可核对。
                return "（摘要生成失败，详见下列各章。）"

        # 固定七章必须都在：缺证据的章节明说缺失，而不是整节消失 ——
        # 实测上游只抽出 5 个维度时，跳过的两章会让人误以为报告是完整的。
        ordered = list(SECTION_ORDER) + [s for s in grouped if s not in SECTION_ORDER]
        cn = "一二三四五六七八九十"

        def render_section(item: tuple[int, str]) -> tuple[str, str]:
            idx, section = item
            evs = grouped.get(section) or []
            title = SECTION_TITLES.get(section, section)
            prefix = f"{cn[idx - 1]}、" if idx <= len(cn) else f"{idx}、"
            head = f"## {prefix}{title}"
            if not evs:
                return head, "本章未获得通过校验的证据，故不输出结论（避免编造）。"
            try:
                body = self.chat(
                    system,
                    f"本次只写一章：{title}。\n\n本章可用证据：\n" + ledger(evs)
                    + conflicts_txt
                    + f"\n\n要求：只输出「{title}」这一章的正文，可用 ### 小标题组织；"
                    "每条事实标注 [E#]；不要写其他章节，不要写参考资料，不要重复章名。",
                    temperature=0.3,
                    max_tokens=3000,
                    json_mode=False,
                    reject_truncated=True,
                )
                return head, body.strip()
            except Exception as exc:  # noqa: BLE001
                # 单节失败必须留痕，不能静默产出残缺报告。
                return head, f"（本章生成失败：{exc}。本章证据仍可在参考资料与证据账本中核对。）"

        tasks = list(enumerate(ordered, 1))
        workers = max(1, int(os.getenv("RESEARCHMIND_WRITE_WORKERS", "5")))
        if workers > 1 and tasks:
            with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks) + 1))) as pool:
                summary_future = pool.submit(render_summary)
                section_futures = [pool.submit(render_section, t) for t in tasks]
                summary_text = summary_future.result()
                rendered = [f.result() for f in section_futures]
        else:
            summary_text = render_summary()
            rendered = [render_section(t) for t in tasks]

        parts += ["## 执行摘要", "", summary_text, ""]
        for head, body in rendered:
            parts += [head, "", body, ""]

        # 参考资料：确定性渲染，省下 token，也保证这一节永远存在。
        parts += ["## 参考资料", ""]
        parts += [ln for ln in render_sources(state)[2:] if ln.strip()]
        parts.append("")
        return "\n".join(parts)


__all__ = ["LLMProvider"]
