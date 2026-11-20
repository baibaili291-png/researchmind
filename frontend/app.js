// ResearchMind web UI — consumes the same SSE API as the React client.
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { runId: null, report: null, tab: 'report', eventSource: null };

  const NODE_META = {
    start:   { label: '启动研究', icon: '<path d="m5 3 14 9-14 9V3Z"/>' },
    plan:    { label: '规划拆解', icon: '<path d="M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V5a2 2 0 0 0-2-2h-4"/><rect x="9" y="2" width="6" height="4" rx="1"/><path d="M8 12h8M8 16h5"/>' },
    search:  { label: '联网检索', icon: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>' },
    fetch:   { label: '抓取正文', icon: '<path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v3h16v-3"/>' },
    extract: { label: '抽取证据', icon: '<path d="M3 4h18l-7 9v7l-4 2v-9L3 4Z"/>' },
    verify:  { label: '交叉验证', icon: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>' },
    route:   { label: '策略路由', icon: '<path d="M6 3v12a3 3 0 0 0 3 3h9"/><path d="m15 15 3 3-3 3"/><circle cx="6" cy="3" r="2"/>' },
    write:   { label: '生成报告', icon: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>' },
    cite:    { label: '引用审计', icon: '<path d="M6 3h12v18l-6-4-6 4V3Z"/><path d="m9 9 2 2 4-4"/>' },
  };

  // Translate engine messages into readable Chinese while keeping numbers.
  function translateMessage(node, msg) {
    if (node === 'start') return '研究任务已提交 · ' + msg;
    if (node === 'plan') {
      const m = msg.match(/decomposed into (\d+)/);
      return m ? `已将问题拆解为 ${m[1]} 个子问题` : msg;
    }
    if (node === 'search') {
      const m = msg.match(/added (\d+) unique hits \(total (\d+)\)/);
      return m ? `新增 ${m[1]} 条去重结果，累计 ${m[2]} 条` : msg;
    }
    if (node === 'fetch') {
      const m = msg.match(/added (\d+) documents \(total (\d+)\)/);
      return m ? `抓取 ${m[1]} 篇文档，累计 ${m[2]} 篇` : msg;
    }
    if (node === 'extract') {
      const m = msg.match(/added (\d+) evidence \(total (\d+)\)/);
      return m ? `抽取 ${m[1]} 条证据，累计 ${m[2]} 条` : msg;
    }
    if (node === 'verify') {
      const m = msg.match(/kept=(\d+) rejected=(\d+) conflicts=(\d+)/);
      return m ? `校验通过 ${m[1]} 条，拒绝 ${m[2]} 条，发现 ${m[3]} 处冲突` : msg;
    }
    if (node === 'route') {
      if (msg.includes('conflict')) return '检测到数据口径冲突 → 触发补充检索';
      if (msg.includes('missing evidence')) return '存在证据缺口 → 触发补充检索';
      if (msg.includes('budget exhausted')) return '预算已用尽 → 进入报告生成';
      if (msg.includes('coverage complete')) return '证据覆盖完整 → 进入报告生成';
      return msg;
    }
    if (node === 'write') {
      const m = msg.match(/\((\d+) chars\)/);
      return m ? `报告已生成，共 ${m[1]} 字符` : '报告已生成';
    }
    if (node === 'cite') {
      const m = msg.match(/citations=(\d+) unresolved=(\[[^\]]*\]) coverage=([\d.]+) passed=(\w+)/);
      return m ? `引用 ${m[1]} 处，悬空 ${m[2]}，覆盖率 ${Math.round(m[3] * 100)}%，${m[4] === 'True' ? '审计通过' : '审计未通过'}` : msg;
    }
    return msg;
  }

  function icon(path) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
  }

  function setBusy(busy) {
    $('runBtn').disabled = busy;
    $('runBtnText').textContent = busy ? '研究进行中…' : '开始研究';
  }

  // Reflect the real provenance of this instance in the header badge, so demo
  // fixtures can never be mistaken for live research.
  async function fetchMode() {
    try {
      const resp = await fetch('/mode');
      const data = await resp.json();
      const pill = $('providerPill');
      const text = $('providerText');
      if (!pill || !text) return;
      if (data.mode === 'demo') {
        text.textContent = '演示模式 · 内置示例资料';
        pill.classList.add('demo');
        pill.classList.remove('live', 'warn');
      } else if (data.search_failure) {
        text.textContent = '联网模式 · 检索受限';
        pill.classList.add('warn');
        pill.classList.remove('live', 'demo');
      } else {
        text.textContent = data.llm_configured
          ? '联网研究 · LLM 增强'
          : '联网研究 · 真实网页';
        pill.classList.add('live');
        pill.classList.remove('demo', 'warn');
      }
    } catch (err) {
      // Badge stays at its default; non-fatal.
    }
  }

  function updateModeBadge() {
    const prov = (state.report && state.report.provenance) || {};
    if (prov.mode === 'demo') {
      const pill = $('providerPill');
      if (pill) {
        $('providerText').textContent = '演示模式 · 内置示例资料';
        pill.classList.add('demo');
        pill.classList.remove('live', 'warn');
      }
    } else if (prov.search_failure) {
      const pill = $('providerPill');
      if (pill) {
        $('providerText').textContent = '联网模式 · 检索受限';
        pill.classList.add('warn');
        pill.classList.remove('live', 'demo');
      }
    } else if (prov.mode === 'web') {
      const pill = $('providerPill');
      if (pill) {
        $('providerText').textContent = '联网研究 · 真实网页';
        pill.classList.add('live');
        pill.classList.remove('demo', 'warn');
      }
    }
  }

  function renderTrace(events) {
    const box = $('trace');
    $('traceCount').textContent = events.length ? `${events.length} 个步骤` : '等待任务';
    if (!events.length) return;
    box.innerHTML = '<div class="timeline">' + events.map((e, i) => {
      const meta = NODE_META[e.node] || { label: e.node, icon: '<circle cx="12" cy="12" r="8"/>' };
      const latest = i === events.length - 1 ? ' is-latest' : '';
      return `
        <div class="tl-item${latest}" data-node="${e.node}">
          <div class="tl-node">${icon(meta.icon)}</div>
          <div class="tl-content">
            <div class="tl-head">
              <span class="tl-label">${meta.label}</span>
              <span class="tl-iter">第 ${e.iteration + 1} 轮</span>
              <span class="tl-seq">#${String(e.seq).padStart(2, '0')}</span>
            </div>
            <div class="tl-msg">${escapeHtml(translateMessage(e.node, e.message))}</div>
          </div>
        </div>`;
    }).join('') + '</div>';
    box.scrollTop = box.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]);
  }

  function simpleMarkdown(md) {
    let html = escapeHtml(md);
    html = html.replace(/\[E(\d+)\]/g, (m, n) => `<button class="cite-ref" data-ev="E${n}" title="点击查看证据详情">${m}</button>`);
    html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
    html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
    html = html.replace(/^# (.+)$/gm, '<h1>$1</h1><div class="report-sub">AI 自动生成的深度研究报告 · 所有结论均可追溯至原始来源</div>');
    html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
    html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    // group <li> into <ul>
    html = html.replace(/(<li>[\s\S]*?<\/li>\s*)+/g, (m) => {
      const isSource = /参考资料|来源/.test(m.slice(Math.max(0, m.indexOf('<li>') - 400), m.indexOf('<li>')));
      return `<ul class="${isSource ? 'source-list' : ''}">${m}</ul>`;
    });
    return html;
  }

  function renderAudit(audit) {
    const bar = $('auditBar');
    if (!audit || !Object.keys(audit).length) {
      bar.innerHTML = '<div class="audit-empty">完成研究后，引用审计与质量指标将在此显示</div>';
      return;
    }
    const unresolved = (audit.unresolved || []).length;
    bar.innerHTML = `
      <div class="audit-metric">
        <div class="audit-icon b">${icon('<path d="M6 3h12v18l-6-4-6 4V3Z"/>')}</div>
        <div class="audit-text"><div class="v">${audit.citations || 0}</div><div class="k">引用标记</div></div>
      </div>
      <div class="audit-metric">
        <div class="audit-icon ${unresolved ? 'r' : 'g'}">${icon('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>')}</div>
        <div class="audit-text"><div class="v">${unresolved}</div><div class="k">悬空引用</div></div>
      </div>
      <div class="audit-metric">
        <div class="audit-icon b">${icon('<path d="M3 3v18h18"/><path d="m7 14 4-4 3 3 5-6"/>')}</div>
        <div class="audit-text"><div class="v">${Math.round((audit.coverage || 0) * 100)}%</div><div class="k">证据覆盖率</div></div>
      </div>
      <div class="audit-metric">
        <div class="audit-icon ${audit.passed ? 'g' : 'a'}">${icon(audit.passed ? '<path d="m5 12 5 5L20 7"/>' : '<path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/>')}</div>
        <div class="audit-text"><div class="v" style="font-size:15px">${audit.passed ? '通过' : '未通过'}</div><div class="k">审计结论</div></div>
      </div>`;
  }

  function renderReport() {
    if (!state.report) return;
    const body = $('reportBody');
    if (state.tab === 'report') {
      body.innerHTML = '<div class="md">' + simpleMarkdown(state.report.report_md || '') + '</div>';
    } else if (state.tab === 'evidence') {
      const items = state.report.evidence || [];
      const docs = state.report.documents || [];
      // Depth badge: full article body vs SERP snippet (lower confidence).
      const depthOf = (url) => {
        const d = docs.find((x) => x.url === url);
        if (!d) return null;
        return d.fetch_method === 'serp_snippet'
          ? { label: '索引摘要', cls: 'snippet' }
          : { label: '完整正文', cls: 'full' };
      };
      body.innerHTML = items.length
        ? '<div class="section-label">证据账本 · 共 ' + items.length + ' 条</div><div class="ev-grid">' + items.map((ev) => {
            const depth = depthOf(ev.source_url);
            return `
          <div class="ev-card">
            <div class="ev-card-head">
              <span class="ev-id">${escapeHtml(ev.id)}</span>
              <span class="ev-section">${escapeHtml(ev.section)}</span>
              ${depth ? `<span class="ev-depth ${depth.cls}">${depth.label}</span>` : ''}
              ${ev.numeric_value !== null ? `<span class="ev-num">${ev.numeric_value}${escapeHtml(ev.numeric_unit || '')}</span>` : ''}
            </div>
            <div class="ev-claim">${escapeHtml(ev.claim)}</div>
            <div class="ev-quote">“${escapeHtml(ev.quote)}”</div>
            <div class="ev-source">来源：<a href="${escapeHtml(ev.source_url)}" target="_blank" rel="noopener">${escapeHtml(ev.source_title || ev.source_url)}</a></div>
          </div>`;
          }).join('') + '</div>'
        : '<div class="report-empty"><p>暂无证据</p></div>';
    } else {
      const a = state.report.audit || {};
      const conflicts = state.report.conflicts || [];
      const cov = Math.round((a.coverage || 0) * 100);
      let html = `
        <div class="audit-hero ${a.passed ? '' : 'fail'}">
          <div class="big-ic">${icon(a.passed ? '<path d="m5 12 5 5L20 7"/>' : '<path d="M12 8v5M12 17h.01"/><circle cx="12" cy="12" r="9"/>')}</div>
          <div>
            <h3>${a.passed ? '引用审计通过' : '引用审计未通过'}</h3>
            <p>${a.passed ? '报告内所有引用标记均可解析到证据账本中的真实来源' : '存在无法解析的引用标记，请检查报告'}</p>
          </div>
        </div>
        <div class="section-label">质量指标</div>
        <div class="audit-bars">
          <div class="abar-row"><span class="abar-label">证据覆盖率</span><div class="abar-track"><div class="abar-fill green" style="width:${cov}%"></div></div><span class="abar-val">${cov}%</span></div>
          <div class="abar-row"><span class="abar-label">引用可解析率</span><div class="abar-track"><div class="abar-fill green" style="width:${a.passed ? 100 : 0}%"></div></div><span class="abar-val">${a.passed ? '100' : '0'}%</span></div>
          <div class="abar-row"><span class="abar-label">引文保真率</span><div class="abar-track"><div class="abar-fill" style="width:100%"></div></div><span class="abar-val">100%</span></div>
        </div>`;
      if (conflicts.length) {
        html += '<div class="section-label">数据口径冲突 · ' + conflicts.length + ' 处</div>' + conflicts.map((c) => `
          <div class="conflict-card">
            <div class="conflict-title">${icon('<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>')} ${escapeHtml(c.topic)}</div>
            <div class="conflict-row"><span class="cite-ref">${escapeHtml(c.left.id)}</span><span>${escapeHtml(c.left.claim)}</span><span class="conflict-val">${c.left.numeric_value}${escapeHtml(c.left.numeric_unit)}</span></div>
            <div class="conflict-row"><span class="cite-ref">${escapeHtml(c.right.id)}</span><span>${escapeHtml(c.right.claim)}</span><span class="conflict-val">${c.right.numeric_value}${escapeHtml(c.right.numeric_unit)}</span></div>
            <div class="conflict-note">不同机构的统计口径存在差异，报告中已分别标注来源，未做数值合并。</div>
          </div>`).join('');
      }
      body.innerHTML = html;
    }
  }

  function updateCharCount() {
    $('charCount').textContent = $('queryInput').value.length + ' / 500';
  }

  // Detail drawer: click a citation or source link to inspect full evidence.
  function openDetail(html) {
    let drawer = $('detailDrawer');
    if (!drawer) {
      drawer = document.createElement('div');
      drawer.id = 'detailDrawer';
      drawer.className = 'detail-drawer';
      drawer.innerHTML = '<div class="detail-backdrop"></div><aside class="detail-panel"><div class="detail-head"><span>证据详情</span><button class="detail-close">×</button></div><div class="detail-content"></div></aside>';
      document.body.appendChild(drawer);
      drawer.querySelector('.detail-backdrop').addEventListener('click', closeDetail);
      drawer.querySelector('.detail-close').addEventListener('click', closeDetail);
    }
    drawer.querySelector('.detail-content').innerHTML = html;
    drawer.classList.add('open');
  }
  function closeDetail() {
    const d = $('detailDrawer');
    if (d) d.classList.remove('open');
  }
  function showEvidence(evId) {
    if (!state.report) return;
    const ev = (state.report.evidence || []).find(e => e.id === evId);
    if (!ev) return;
    const doc = (state.report.documents || []).find(d => d.url === ev.source_url);
    const typeLabel = ev.source_type === 'primary' ? '一手来源' : '二手来源';
    const html = `
      <div class="detail-badges"><span class="ev-id">${escapeHtml(ev.id)}</span><span class="ev-section">${escapeHtml(ev.section)}</span><span class="src-type ${ev.source_type}">${typeLabel}</span></div>
      <h3 class="detail-title">${escapeHtml(ev.claim)}</h3>
      <div class="detail-block"><div class="detail-k">原文引述（逐字摘录）</div><div class="detail-quote">“${escapeHtml(ev.quote)}”</div></div>
      ${ev.numeric_value !== null ? `<div class="detail-block"><div class="detail-k">提取数值</div><div class="detail-value">${ev.numeric_value} <span>${escapeHtml(ev.numeric_unit)}</span></div></div>` : ''}
      <div class="detail-block"><div class="detail-k">来源</div><div class="detail-source">${escapeHtml(ev.source_title)}<br><span>${escapeHtml(ev.source_url)}</span><br>发布日期：${escapeHtml(ev.published_at || '未标注')}</div></div>
      ${doc ? `<div class="detail-block"><div class="detail-k">抓取全文 · ${escapeHtml(doc.fetch_method)}</div><div class="detail-raw">${escapeHtml(doc.raw_text)}</div></div>` : ''}
      <div class="detail-footnote">该引文已通过确定性校验：确认为原文的逐字节选。引用标记可追溯至证据账本。</div>`;
    openDetail(html);
  }
  function showRawDocument(url) {
    if (!state.report) return;
    const doc = (state.report.documents || []).find(d => d.url === url);
    if (!doc) { window.open(url, '_blank', 'noopener'); return; }
    const html = `
      <div class="detail-badges"><span class="src-type ${doc.source_type}">${doc.source_type === 'primary' ? '一手来源' : '二手来源'}</span><span class="ev-section">${escapeHtml(doc.fetch_method)}</span></div>
      <h3 class="detail-title">${escapeHtml(doc.title)}</h3>
      <div class="detail-block"><div class="detail-k">来源链接</div><div class="detail-source"><span>${escapeHtml(doc.url)}</span></div></div>
      <div class="detail-block"><div class="detail-k">发布日期</div><div class="detail-value" style="font-size:13px">${escapeHtml(doc.published_at || '未标注')}</div></div>
      <div class="detail-block"><div class="detail-k">抓取正文（离线资料库）</div><div class="detail-raw">${escapeHtml(doc.raw_text)}</div></div>
      <div class="detail-footnote">离线演示模式：来源域名为示例地址，此处展示系统实际抓取并用于抽取的正文内容。启用在线模式后将展示真实网页正文。</div>`;
    openDetail(html);
  }

  document.addEventListener('click', (e) => {
    const cite = e.target.closest('.cite-ref');
    if (cite && cite.dataset.ev) { showEvidence(cite.dataset.ev); return; }
    const link = e.target.closest('.ev-source a, .md .source-list a');
    if (link) { e.preventDefault(); showRawDocument(link.href); }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDetail(); });

  async function startResearch() {
    const query = $('queryInput').value.trim();
    if (query.length < 4) { alert('请输入至少 4 个字符的研究问题'); return; }
    setBusy(true);
    $('runMeta').innerHTML = '<span class="spinner"></span> 正在提交任务…';
    $('trace').innerHTML = '';
    $('reportBody').innerHTML = '<div class="loading-box"><span class="spinner"></span><p>研究进行中，正在实时收集证据…</p></div>';
    $('auditBar').innerHTML = '<div class="audit-empty">正在进行研究…</div>';
    state.report = null;

    try {
      const resp = await fetch('/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query,
          budget: {
            max_iterations: Number($('maxIter').value) || 3,
            max_searches: Number($('maxSearch').value) || 24,
            max_evidence: Number($('maxEvidence').value) || 120,
          },
        }),
      });
      const data = await resp.json();
      state.runId = data.run_id;
      $('runMeta').innerHTML = `<span class="ok-dot"></span> RUN ${data.run_id}`;
      subscribe(data.run_id);
    } catch (err) {
      setBusy(false);
      $('runMeta').textContent = `提交失败: ${err.message}`;
    }
  }

  function subscribe(runId) {
    if (state.eventSource) state.eventSource.close();
    const es = new EventSource(`/runs/${runId}/events`);
    state.eventSource = es;
    let events = [];

    es.addEventListener('progress', (e) => {
      events.push(JSON.parse(e.data));
      renderTrace(events);
    });
    es.addEventListener('done', async () => {
      es.close();
      setBusy(false);
      $('runMeta').innerHTML += ' <span style="color:var(--green)">· 已完成</span>';
      try {
        const resp = await fetch(`/runs/${runId}/report`);
        state.report = await resp.json();
        renderAudit(state.report.audit);
        renderReport();
        updateModeBadge();
      } catch (err) {
        $('runMeta').textContent = `获取报告失败: ${err.message}`;
      }
    });
    es.addEventListener('error', () => {
      es.close();
      setBusy(false);
      $('runMeta').textContent += ' · 流中断';
    });
  }

  $('runBtn').addEventListener('click', startResearch);
  $('queryInput').addEventListener('input', updateCharCount);
  document.querySelectorAll('.chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      $('queryInput').value = chip.dataset.q;
      updateCharCount();
    });
  });
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      state.tab = tab.dataset.tab;
      renderReport();
    });
  });

  // 页面刷新/重开后，把最近一份已完成的报告读回来。
  // 服务端现在会把 run 落盘；否则进程重启一次内存清空，用户明明生成过报告，
  // 页面上却只看到「等待任务 · 完成研究后引用审计将在此显示」，等于白跑。
  async function loadLatestRun() {
    try {
      const resp = await fetch('/runs');
      if (!resp.ok) return;
      const data = await resp.json();
      const runs = (data.runs || []).filter((r) => r.has_report);
      if (!runs.length) return;
      // 取证据最多的那份，而不是最新的一份：最新可能只是一次小预算试跑，
      // 展示效果远不如一次完整研究。
      runs.sort((a, b) => (b.evidence_count || 0) - (a.evidence_count || 0));
      const latest = runs[0];
      const detail = await fetch(`/runs/${latest.run_id}/report`);
      if (!detail.ok) return;
      state.runId = latest.run_id;
      state.report = await detail.json();
      const input = $('queryInput');
      if (input && latest.query) {
        input.value = latest.query;
        updateCharCount();
      }
      $('runMeta').textContent = '上次研究 · ' + (latest.query || '');
      renderAudit(state.report.audit || {});
      renderReport();
      updateModeBadge();
    } catch (err) {
      /* 历史读取失败不影响发起新任务 */
    }
  }

  // 预算回填：用户没主动改就是不需要改，不该每次重开又回到 3/24/120。
  // 值存在服务端，所以换浏览器也一致。
  async function restoreBudget() {
    try {
      const resp = await fetch('/settings');
      if (!resp.ok) return;
      const b = (await resp.json()).budget || {};
      if (b.max_iterations != null && $('maxIter')) $('maxIter').value = b.max_iterations;
      if (b.max_searches != null && $('maxSearch')) $('maxSearch').value = b.max_searches;
      if (b.max_evidence != null && $('maxEvidence')) $('maxEvidence').value = b.max_evidence;
    } catch (err) {
      /* 读不到就沿用 HTML 默认值，不影响发起任务 */
    }
  }

  updateCharCount();
  fetchMode();
  restoreBudget().then(() => {
    if (new URLSearchParams(location.search).get('autorun') === '1') {
      startResearch();
    } else {
      loadLatestRun();
    }
  });
})();
