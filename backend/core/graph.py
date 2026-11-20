"""The LangGraph-style state machine, implemented with plain Python.

Production node bodies call LangGraph's `StateGraph` + `AsyncPostgresSaver`.
Here the same node/edge structure is explicit so the control flow — especially
the loop back from verify to search — is easy to inspect and test.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .adapters import ResearchAdapters
from .models import (
    Budget, Document, Evidence, ResearchState, SearchHit, SubQuestion,
)
from .verify import evidence_belongs_to_section, validate_citations, verify_evidence


class ResearchGraph:
    def __init__(self, adapters: ResearchAdapters, budget: Budget | None = None) -> None:
        self.adapters = adapters
        self.budget = budget or Budget()
        self.evidence_seq = 0

    # -- node: plan ----------------------------------------------------------
    def plan(self, state: ResearchState) -> None:
        state.plan = self.adapters.plan(state.query)
        state.llm_calls += 1
        state.log("plan", f"decomposed into {len(state.plan)} sub-questions")

    # -- node: search --------------------------------------------------------
    def search(self, state: ResearchState) -> None:
        seen = {h.key for h in state.search_results}
        added = 0
        calls = 0
        if state.follow_up_queries:
            tasks = [(q, "") for q in state.follow_up_queries]
        else:
            tasks = [(q, sq.id) for sq in state.plan for q in sq.queries]
        for query, sqid in tasks:
            if state.search_calls + calls >= self.budget.max_searches:
                break
            if len(state.search_results) >= self.budget.max_hits:
                break
            calls += 1
            for hit in self.adapters.search(query, sqid, 6):
                if hit.key in seen:
                    # Same source answers multiple sub-questions: keep all links.
                    existing = next(h for h in state.search_results if h.key == hit.key)
                    if sqid:
                        existing.sub_question_ids.add(sqid)
                    continue
                if len(state.search_results) >= self.budget.max_hits:
                    break
                if sqid:
                    hit.sub_question_ids.add(sqid)
                seen.add(hit.key)
                state.search_results.append(hit)
                added += 1
        state.search_calls += calls
        state.log(
            "search",
            f"{calls} queries -> {added} new sources (total {len(state.search_results)})",
        )

    # -- node: fetch ---------------------------------------------------------
    def _fair_order(self, state: ResearchState) -> list[SearchHit]:
        """Interleave hits by sub-question instead of taking them in plan order.

        Same defect as the evidence quota, one stage earlier: with 14 queries ×
        6 hits and a cap of 40 fetches, the first few queries filled every fetch
        slot, so 4 of 7 dimensions never got a single page. Round-robin by
        sub-question guarantees each dimension gets its turn at the cap.
        """
        plan_ids = [sq.id for sq in state.plan]
        if not plan_ids:
            return list(state.search_results)
        quota = max(1, self.budget.max_fetches // len(plan_ids))
        by_sq: dict[str, list[SearchHit]] = {sid: [] for sid in plan_ids}
        for hit in state.search_results:
            ids = hit.sub_question_ids or ({hit.sub_question_id} if hit.sub_question_id else set())
            for sid in ids:
                if sid in by_sq:
                    by_sq[sid].append(hit)

        ordered: list[SearchHit] = []
        queued: set[str] = set()
        taken = {sid: 0 for sid in plan_ids}
        # True round-robin (one hit per dimension per round), not quota-sized
        # chunks: if the cap is reached mid-way, chunks would still starve the
        # tail of the plan, while round-robin degrades evenly.
        progressed = True
        while progressed:
            progressed = False
            for sid in plan_ids:
                if taken[sid] >= quota:
                    continue
                for hit in by_sq[sid]:
                    if hit.key in queued:
                        continue
                    queued.add(hit.key)
                    ordered.append(hit)
                    taken[sid] += 1
                    progressed = True
                    break
        for hit in state.search_results:
            if hit.key not in queued:
                queued.add(hit.key)
                ordered.append(hit)
        return ordered

    def fetch(self, state: ResearchState) -> None:
        known_keys = {d.key for d in state.documents}
        known_hashes = {d.content_hash for d in state.documents}
        # Keywords per sub-question feed the relevance gate in fetch().
        kw_by_sq = {sq.id: sq.keywords for sq in state.plan}

        pending: list[tuple[SearchHit, list[str]]] = []
        for hit in self._fair_order(state):
            if len(pending) + len(state.documents) >= self.budget.max_fetches:
                break
            if hit.key in known_keys:
                continue
            sqids = hit.sub_question_ids or ({hit.sub_question_id} if hit.sub_question_id else set())
            keywords: list[str] = []
            for sqid in sqids:
                keywords.extend(kw_by_sq.get(sqid, []))
            pending.append((hit, keywords))

        # Concurrent fetch when the adapter supports it: per-domain pacing is
        # preserved inside FetchChain, so politeness is unchanged while
        # different hosts download in parallel.
        fetch_many = getattr(self.adapters, "fetch_many", None)
        if callable(fetch_many) and len(pending) > 1:
            results = fetch_many(pending)
        else:
            results = [
                self.adapters.fetch(hit, keywords=kw) if kw else self.adapters.fetch(hit)
                for hit, kw in pending
            ]

        added = 0
        for doc in results:
            if doc is None:
                continue
            if len(state.documents) >= self.budget.max_fetches:
                break
            if doc.key in known_keys:
                continue
            if doc.content_hash in known_hashes:
                known_keys.add(doc.key)
                continue
            known_keys.add(doc.key)
            known_hashes.add(doc.content_hash)
            state.documents.append(doc)
            added += 1
        stats = getattr(self.adapters, "fetch_stats", None)
        detail = f" ({dict(stats)})" if stats else ""
        state.log("fetch", f"added {added} documents (total {len(state.documents)}){detail}")

    def _safe_extract(self, sq: SubQuestion, doc: Document) -> tuple[list[Evidence], bool]:
        """返回 (evidence, ok)。

        ok=False 表示**这次调用失败了**，和「这一页没有相关事实」是两件事。
        早期版本两者都返回空列表，于是并发下被限流的几篇文档被当成「无内容」，
        对应章节整节变空，而运行状态仍然显示成功 —— 表现为报告里五章全是
        「本章未获得通过校验的证据」。
        """
        for attempt in range(2):
            try:
                return self.adapters.extract(doc, sq), True
            except Exception:
                if attempt == 0:
                    time.sleep(1.0)
        return [], False

    # -- node: extract -------------------------------------------------------
    def extract(self, state: ResearchState) -> None:
        indexed: dict[str, list[Document]] = {}
        for doc in state.documents:
            indexed.setdefault(doc.key, []).append(doc)
        results_by_subq: dict[str, list[Document]] = {}
        for hit in state.search_results:
            for doc in indexed.get(hit.key, []):
                sqids = hit.sub_question_ids or ({hit.sub_question_id} if hit.sub_question_id else set())
                for sqid in sqids:
                    results_by_subq.setdefault(sqid, []).append(doc)
        # 每个子问题最多喂这么多文档给抽取器。一篇文档平均出 2-4 条可用证据，
        # 而每个子问题的配额是 quota 条 —— 超出的部分即使抽出来也会被 quota 或
        # 去重挡掉，但每次 6s 的调用成本已经付出去了。实测一次运行 pairs 冲到
        # 120 对（6 子问题 × 20 文档），仅抽取就耗掉 247s。
        quota = max(1, self.budget.max_evidence // max(1, len(state.plan)))
        docs_per_sq = max(4, quota // 2 + 2)
        # 排除「已经对该子问题产出过证据」的 (来源, 子问题) 组合。
        # 早先按 source_url 全局排除是个严重错误：一个文档只要在别的子问题下
        # 抽过，当前子问题就再也取不到它，最后表现为报告里五章整节空白。
        done_pairs = {(e.source_url, e.sub_question_id) for e in state.evidence}

        pairs: list[tuple[SubQuestion, Document]] = []
        if results_by_subq:
            for sq in state.plan:
                for doc in results_by_subq.get(sq.id, [])[:docs_per_sq]:
                    if (doc.url, sq.id) in done_pairs:
                        continue
                    pairs.append((sq, doc))
        else:
            for sq in state.plan:
                for doc in state.documents[:docs_per_sq]:
                    if (doc.url, sq.id) in done_pairs:
                        continue
                    pairs.append((sq, doc))

        added = 0
        # Dedup globally on (source, quote). The previous key included
        # sub_question_id, so one source that answered two sub-questions emitted
        # the same verbatim sentence twice (observed as E1/E9 in a live run),
        # padding the report with duplicates. First sub-question wins.
        seen = {(e.source_url, e.quote) for e in state.evidence}
        skipped = 0
        # Evidence is allocated per sub-question, not first-come-first-served.
        # Measured 2026-11-20: with a flat max_evidence of 120, the first two
        # dimensions filled the quota, `Budget.exhausted()` then returned True on
        # the very first iteration, and the report shipped with 2 of 7 sections
        # researched (38 documents fetched, 5 dimensions silently dropped). A fair
        # share guarantees every planned dimension is represented before any one
        # of them can monopolise the ledger.
        per_sq: dict[str, int] = {}

        # LLM extraction is I/O-bound: measured 6.1s per call, and one iteration
        # issues one call per (sub-question, document) pair. Serially that burns
        # ~4 minutes per iteration, and the time budget is only checked between
        # iterations -- a reported "half hour" run was 2-3 of these back to
        # back. Extract concurrently, then book the results on the main thread
        # in pair order, so seen / per_sq / evidence_seq need no locking and
        # evidence ids stay deterministic across runs.
        # 3 并发是实测出来的安全值：8 并发时 DeepSeek 端会限流，失败的调用被
        # 当成「无证据」，直接导致整章空白。宁可慢一点，也不能再出空章节。
        workers = max(1, int(os.getenv("RESEARCHMIND_EXTRACT_WORKERS", "3")))
        total = len(pairs)
        step = max(1, total // 8)
        done = [0]
        counter_lock = threading.Lock()
        log_lock = threading.Lock()

        failures = [0]

        def task(pair: tuple[SubQuestion, Document]) -> list[Evidence]:
            sq, doc = pair
            evs, ok = self._safe_extract(sq, doc)
            with counter_lock:
                done[0] += 1
                n = done[0]
                if not ok:
                    failures[0] += 1
            # Extraction is the longest stretch of the pipeline (6.1s per call).
            # Without progress events the page sits still for minutes and the
            # run looks hung -- that, not the total duration, is what makes it
            # feel bad. ~8 updates per iteration is enough to look alive.
            if n == total or n % step == 0:
                with log_lock:
                    state.log("extract", f"正在抽取证据 {n}/{total} 篇来源")
            return evs

        if workers > 1 and total > 1:
            with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
                batches = list(pool.map(task, pairs))
        else:
            batches = [task(p) for p in pairs]

        for (sq, doc), extracted in zip(pairs, batches):
            if len(state.evidence) >= self.budget.max_evidence:
                break
            if per_sq.get(sq.id, 0) >= quota:
                continue
            for ev in extracted:
                if per_sq.get(sq.id, 0) >= quota:
                    break
                key = (ev.source_url, ev.quote)
                if key in seen:
                    continue
                # A number has to substantiate the section it is filed under;
                # otherwise the report cites a benchmark score as market size.
                ok, reason = evidence_belongs_to_section(ev)
                if not ok:
                    skipped += 1
                    continue
                seen.add(key)
                self.evidence_seq += 1
                ev.id = f"E{self.evidence_seq}"
                state.evidence.append(ev)
                per_sq[sq.id] = per_sq.get(sq.id, 0) + 1
                added += 1
        state.llm_calls += max(0, len(pairs) - failures[0])
        if failures[0]:
            state.log(
                "extract",
                f"警告：{failures[0]}/{len(pairs)} 次抽取调用失败，"
                "这些来源未产出任何证据（不等于页面无内容）",
            )
        covered = len(per_sq)
        state.log(
            "extract",
            f"added {added} evidence across {covered}/{len(state.plan)} dimensions "
            f"(quota {quota}/dimension, total {len(state.evidence)})"
            + (f", {skipped} off-section dropped" if skipped else ""),
        )

    # -- node: verify --------------------------------------------------------
    def verify(self, state: ResearchState) -> None:
        verify_evidence(state)

    # -- node: route (conditional edge) --------------------------------------
    def route(self, state: ResearchState) -> str:
        """Return 'search' to loop, or 'write' to finish."""
        if self.budget.exhausted(state):
            state.log("route", "budget exhausted -> write")
            return "write"
        missing = [sq for sq in state.plan
                   if not any(e.sub_question_id == sq.id for e in state.evidence)]
        if state.conflicts:
            c = state.conflicts[0]
            state.follow_up_queries = [
                f"{c.topic} 统计口径 差异 说明",
                f"{c.topic} 原始数据 来源",
            ]
            state.log("route", f"conflict on {c.topic} -> follow-up search")
            return "search"
        if missing:
            state.follow_up_queries = list(missing[0].queries)
            state.log("route", f"missing evidence for {missing[0].id} -> follow-up search")
            return "search"
        state.log("route", "coverage complete -> write")
        return "write"

    # -- node: write ---------------------------------------------------------
    def write(self, state: ResearchState) -> None:
        state.report = self.adapters.write(state)
        state.llm_calls += 1
        state.log("write", f"report generated ({len(state.report)} chars)")

    # -- node: cite (final gate) ---------------------------------------------
    def cite(self, state: ResearchState) -> dict:
        audit = validate_citations(state.report, state.evidence)
        state.log(
            "cite",
            f"citations={audit['citations']} unresolved={audit['unresolved']} "
            f"coverage={audit['coverage']} passed={audit['passed']}",
        )
        return audit

    # -- orchestration -------------------------------------------------------
    def run(self, query: str, on_event=None) -> tuple[ResearchState, dict]:
        state = ResearchState(query=query, budget=self.budget, on_event=on_event)
        state.log("start", query)
        started = time.time()

        # Record provenance before writing so the report can disclose whether
        # its sources are live pages or offline demo fixtures.
        describe = getattr(self.adapters, "describe", None)
        if callable(describe):
            try:
                state.provenance = describe()
            except Exception:
                state.provenance = {}

        self.plan(state)
        _timing = os.getenv("RESEARCHMIND_DEBUG_TIMING") == "1"

        def _mark(label: str, t0: float) -> None:
            if _timing:
                print(f"[TIMING] {label}: {time.time() - t0:.1f}s", flush=True)

        while True:
            if time.time() - started > self.budget.time_budget_s:
                state.log("route", "time budget exhausted -> write")
                break
            t = time.time(); self.search(state); _mark(f"iter{state.iteration} search", t)
            t = time.time(); self.fetch(state); _mark(f"iter{state.iteration} fetch", t)
            t = time.time(); self.extract(state); _mark(f"iter{state.iteration} extract", t)
            t = time.time(); self.verify(state); _mark(f"iter{state.iteration} verify", t)
            state.iteration += 1
            if self.route(state) == "write":
                break

        # Refresh after the loop: search may have failed mid-run.
        if callable(describe):
            try:
                state.provenance = describe()
            except Exception:
                pass

        t = time.time(); self.write(state); _mark("write", t)
        audit = self.cite(state)
        state.provenance = {
            **state.provenance,
            "documents": len(state.documents),
            "evidence": len(state.evidence),
            "elapsed_s": round(time.time() - started, 1),
        }
        return state, audit
