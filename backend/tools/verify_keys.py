"""One-command credential self-check.

    python -m backend.tools.verify_keys

Answers a single question: with the keys currently in .env, can this system do
real research right now, and if not, which layer is the blocker?

Every row is a live request. Nothing is inferred from the presence of a key:
a key that is configured but rejected must show up as FAILED with the server's
own message, because that is exactly the failure that used to masquerade as
"the engines found nothing".
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request

from backend.config import settings

OK = "OK  "
FAIL = "FAIL"
SKIP = "—   "


def _mask(key: str) -> str:
    return f"...{key[-4:]}" if len(key) >= 4 else "(empty)"


def _line(name: str, status: str, detail: str) -> None:
    print(f"  {status}  {name:<22} {detail}")


def check_llm() -> bool:
    if not settings.llm_api_key:
        _line("LLM (chat)", SKIP, "未配置 RESEARCHMIND_LLM_API_KEY")
        return False
    import json

    payload = json.dumps({
        "model": settings.llm_model,
        "messages": [{"role": "user", "content": "回复OK"}],
        "max_tokens": 512,
    }).encode("utf-8")
    req = urllib.request.Request(
        settings.llm_base_url.rstrip("/") + "/chat/completions", data=payload,
        headers={"Authorization": f"Bearer {settings.llm_api_key}",
                 "Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=settings.llm_timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        took = time.time() - t0
        model = body.get("model") or settings.llm_model
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        if not (msg.get("content") or "").strip():
            _line("LLM (chat)", FAIL,
                  f"{model} 返回空 content（reasoning 吃掉了预算，可调大 LLM_MAX_TOKENS）")
            return False
        _line("LLM (chat)", OK, f"{model} · {took:.1f}s")
        return True
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:120]
        _line("LLM (chat)", FAIL, f"HTTP {exc.code} · {detail}")
    except Exception as exc:  # noqa: BLE001
        _line("LLM (chat)", FAIL, f"{type(exc).__name__}: {str(exc)[:100]}")
    return False


def check_deepseek_search() -> bool:
    if not settings.deepseek_api_key:
        _line("DeepSeek Search", SKIP,
              "未配置 RESEARCHMIND_DEEPSEEK_API_KEY（LLM key 不会被借用）")
        return False
    from backend.providers.search import DeepSeekSearchProvider

    provider = DeepSeekSearchProvider(settings.deepseek_api_key,
                                      settings.deepseek_base_url)
    try:
        hits = provider.search("2026年中国AI Agent市场规模", limit=3)
    except Exception as exc:  # noqa: BLE001
        _line("DeepSeek Search", FAIL, f"{type(exc).__name__}: {str(exc)[:110]}")
        return False
    _line("DeepSeek Search", OK, f"{len(hits)} 条 · 首条 {hits[0].url[:56]}")

    try:
        got = provider.fetch_page(hits[0].url)
    except Exception as exc:  # noqa: BLE001
        _line("DeepSeek Fetch", FAIL, f"{type(exc).__name__}: {str(exc)[:110]}")
        return True
    if got:
        _line("DeepSeek Fetch", OK, f"{len(got[1])} 字 Markdown")
    else:
        _line("DeepSeek Fetch", FAIL, "返回空正文（该 URL 可能不可抓）")
    return True


def check_tavily() -> bool:
    if not settings.search_api_key:
        _line("Tavily Search", SKIP, "未配置 RESEARCHMIND_SEARCH_API_KEY")
        return False
    from backend.providers.search import TavilyProvider

    try:
        provider = TavilyProvider()
    except Exception as exc:  # noqa: BLE001
        _line("Tavily Search", FAIL, str(exc)[:110])
        return False
    try:
        hits = provider.search("2026年中国AI Agent市场规模", limit=3)
    except Exception as exc:  # noqa: BLE001
        _line("Tavily Search", FAIL, f"{type(exc).__name__}: {str(exc)[:110]}")
        return False
    _line("Tavily Search", OK, f"{len(hits)} 条 · 首条 {hits[0].url[:56]}")

    got = provider.extract(hits[0].url)
    if got:
        _line("Tavily Extract", OK, f"{len(got[1])} 字正文")
    else:
        _line("Tavily Extract", FAIL, "返回空正文（配额或该站点拒绝）")
    return True


def check_engine_chain() -> bool:
    """The full chain as the pipeline will actually use it, in order.

    Labelled by what it is, not "free engines": now that a paid engine can sit at
    the head of the chain, calling the row "免费引擎" while it reports
    "由 deepseek 提供" is simply wrong.
    """
    from backend.providers.search import ChainedSearch

    chain = ChainedSearch()
    order = [getattr(e, "name", "?") for e in chain.engines]
    try:
        hits = chain.search("2026年中国AI Agent市场规模", limit=3)
    except Exception as exc:  # noqa: BLE001
        _line("检索链", FAIL, f"{type(exc).__name__}: {str(exc)[:90]}")
        return False
    if hits:
        paid = chain.last_engine in ("deepseek", "tavily")
        _line("检索链", OK,
              f"{len(hits)} 条 · 由 {chain.last_engine} 提供"
              f"{'（付费）' if paid else '（免费，会限流）'} · 顺序 {'>'.join(order[:4])}")
        return True
    _line("检索链", FAIL, "本轮全部失败：" + "; ".join(chain.errors[-2:])[:110])
    return False


def main() -> int:
    print("ResearchMind 凭证自检")
    print(f"  .env 读取：LLM key={_mask(settings.llm_api_key)} "
          f"DeepSeek 检索 key={_mask(settings.deepseek_api_key)} "
          f"Tavily key={_mask(settings.search_api_key)}")
    print(f"  LLM 端点：{settings.llm_base_url} | 模型：{settings.llm_model}")
    print(f"  联网模式：{'web' if settings.use_web else 'demo'} | "
          f"允许外部请求：{settings.use_live_providers}\n")

    llm = check_llm()
    ds = check_deepseek_search()
    tv = check_tavily()
    free = check_engine_chain()

    print()
    searchable = ds or tv
    if searchable:
        print("结论：检索走付费通道，可以稳定跑完整研究（维度覆盖不再受免费引擎限流影响）。")
    elif free:
        print("结论：只有免费引擎可用 —— 单次运行能出结果，但同一 IP 连跑几轮会")
        print("      被同时限流，维度覆盖不稳定。要稳定请配 DeepSeek 或 Tavily 的 key。")
    else:
        print("结论：当前没有任何可用检索通道，研究必然产出 0 条证据。")
        print("      先解决检索，再谈报告质量。")
    if not llm:
        print("注意：LLM 不可用，规划/抽取/写作会回落到确定性实现（能跑，质量较低）。")
    return 0 if (searchable or free) else 1


if __name__ == "__main__":
    raise SystemExit(main())
