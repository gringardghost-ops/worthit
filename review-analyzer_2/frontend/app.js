(() => {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const EXAMPLES = {
    place: ['Cafe Goodluck Pune', 'Leopold Cafe Colaba Mumbai', 'teamLab Planets Tokyo'],
    product: ['Sony WH-1000XM5', 'Kindle Paperwhite 12th generation', 'Instant Pot Duo 7-in-1'],
  };
  const PLACEHOLDERS = {
    place: 'A cafe, hotel or attraction',
    product: 'A product name or model number',
  };
  const HINTS = { place: 'add the city, it helps a lot', product: 'the exact model name works best' };
  const TYPE_LABEL = { place: 'Place or experience', product: 'Product' };

  const state = { type: 'place', busy: false, last: null, lastRequest: null, controller: null };
  const els = {
    form: $('#search-form'), input: $('#query'), btn: $('#analyze-btn'), chips: $('#chips'),
    recent: $('#recent'), recentList: $('#recent-list'),
    loading: $('#loading'), elapsed: $('#elapsed'), steps: $$('.step'),
    error: $('#error'), errorTitle: $('#error-title'), errorText: $('#error-text'), retry: $('#retry-btn'),
    results: $('#results'), toast: $('#toast'),
    typeToggle: $('#type-toggle'), insightTabs: $('#insight-tabs'),
  };

  /* ---------- helpers ---------- */
  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else if (k.startsWith('data-')) node.setAttribute(k, v);
      else node[k] = v;
    }
    for (const c of [].concat(children)) if (c != null) node.append(c);
    return node;
  }
  const tierOf = (score) => (score < 5 ? 'low' : score < 7.5 ? 'mid' : 'high');
  let toastTimer;
  function toast(message) {
    els.toast.textContent = message;
    els.toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove('show'), 2200);
  }

  /* ---------- sliding pill for segmented controls ---------- */
  function movePill(container) {
    const active = $('button[aria-selected="true"]', container);
    if (!active || !active.offsetWidth) return;
    container.style.setProperty('--pill-x', active.offsetLeft + 'px');
    container.style.setProperty('--pill-w', active.offsetWidth + 'px');
  }
  const allSegmented = () => $$('.segmented');
  window.addEventListener('resize', () => allSegmented().forEach(movePill));
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => allSegmented().forEach(movePill));

  function selectTab(container, button) {
    $$('button', container).forEach((b) => b.setAttribute('aria-selected', String(b === button)));
    movePill(container);
  }

  /* ---------- theme ---------- */
  const themeBtn = $('#theme-toggle');
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    themeBtn.setAttribute('aria-label', theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode');
    try { localStorage.setItem('worth-it-theme', theme); } catch (e) { /* storage unavailable */ }
  }
  themeBtn.addEventListener('click', () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');

  /* ---------- health pill ---------- */
  async function loadHealth() {
    const pill = $('#status-pill');
    const text = $('#status-text');
    try {
      const res = await fetch('/api/health', { cache: 'no-store' });
      const h = await res.json();
      if (h.serpapi && h.gemini) { pill.dataset.state = 'ok'; text.textContent = 'API keys connected'; }
      else {
        const missing = [!h.serpapi && 'SerpApi', !h.gemini && 'Gemini'].filter(Boolean).join(' and ');
        pill.dataset.state = h.serpapi || h.gemini ? 'warn' : 'error';
        text.textContent = `${missing} key missing`;
      }
    } catch (e) {
      pill.dataset.state = 'error';
      text.textContent = 'Server offline';
    }
  }

  /* ---------- type toggle, chips, recent ---------- */
  function renderChips() {
    els.chips.replaceChildren(...EXAMPLES[state.type].map((q) => {
      const b = el('button', { class: 'chip', type: 'button', text: q });
      b.addEventListener('click', () => { els.input.value = q; analyze(); });
      return b;
    }));
  }
  function setType(type) {
    state.type = type;
    selectTab(els.typeToggle, $(`button[data-type="${type}"]`, els.typeToggle));
    els.input.placeholder = PLACEHOLDERS[type];
    $('#hint-text').textContent = HINTS[type];
    renderChips();
  }
  $$('button', els.typeToggle).forEach((b) => b.addEventListener('click', () => setType(b.dataset.type)));

  async function loadRecent() {
    try {
      const res = await fetch('/api/recent', { cache: 'no-store' });
      const items = await res.json();
      if (!Array.isArray(items) || !items.length) { els.recent.hidden = true; return; }
      els.recentList.replaceChildren(...items.map((it) => {
        const score = typeof it.worth_it_score === 'number' ? it.worth_it_score : null;
        const b = el('button', { class: 'recent-item', type: 'button', title: `${it.entity_name} (${TYPE_LABEL[it.type] || it.type})` }, [
          el('span', { class: 'name', text: it.entity_name }),
          score != null ? el('span', { class: 'score', text: score.toFixed(1) }) : null,
        ]);
        if (score != null) b.dataset.tier = tierOf(score);
        b.addEventListener('click', () => { setType(it.type); els.input.value = it.query; analyze(); });
        return b;
      }));
      els.recent.hidden = false;
    } catch (e) { els.recent.hidden = true; }
  }

  /* ---------- loading experience ---------- */
  let timerId = null;
  function startLoading() {
    els.error.hidden = true;
    els.results.hidden = true;
    els.loading.hidden = false;
    els.btn.disabled = true;
    const started = performance.now();
    const setSteps = (active) => els.steps.forEach((s, i) => { s.dataset.state = i < active ? 'done' : i === active ? 'active' : 'pending'; });
    setSteps(0);
    timerId = setInterval(() => {
      const t = (performance.now() - started) / 1000;
      els.elapsed.textContent = t.toFixed(1) + 's';
      setSteps(t < 2.5 ? 0 : t < 12 ? 1 : 2);
    }, 100);
  }
  function stopLoading() {
    clearInterval(timerId);
    els.steps.forEach((s) => { s.dataset.state = 'done'; });
    els.loading.hidden = true;
    els.btn.disabled = false;
  }

  /* ---------- analysis ---------- */
  function errorMessage(status, body) {
    const d = body && body.detail;
    if (Array.isArray(d) && d.length) return 'Check your input: ' + (d[0].msg || 'invalid value').replace(/^Value error, /, '');
    if (typeof d === 'string') return d;
    return `The server returned an error (${status}).`;
  }
  const ERROR_TITLES = { 404: "We couldn't find that", 422: 'Not enough reviews to judge', 429: 'Rate limit reached', 503: 'Setup needed', 504: 'A review source is slow' };

  async function analyze({ force = false } = {}) {
    const query = els.input.value.trim().replace(/\s+/g, ' ');
    if (query.length < 2) { els.input.focus(); toast('Enter at least 2 characters'); return; }
    if (state.busy) return;
    state.busy = true;
    state.lastRequest = { query, type: state.type };
    state.controller = new AbortController();
    // The API returns its own 504 after 105s; this is a final safety net if the connection stalls.
    const timeout = setTimeout(() => state.controller.abort(), 120000);
    startLoading();
    try {
      const res = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, type: state.type, force_refresh: force }),
        signal: state.controller.signal,
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) { showError(ERROR_TITLES[res.status] || "That didn't work", errorMessage(res.status, body)); return; }
      state.last = body;
      stopLoading();
      renderResult(body);
      loadRecent();
    } catch (e) {
      showError(e.name === 'AbortError' ? 'This is taking too long' : "Can't reach the server",
        e.name === 'AbortError' ? 'The server did not finish in time. A review source may be slow; try again with a more specific name.' : 'Make sure the launcher is still running, then try again.');
    } finally {
      clearTimeout(timeout);
      clearInterval(timerId);
      els.loading.hidden = true;
      els.btn.disabled = false;
      state.busy = false;
    }
  }
  function showError(title, text) {
    els.errorTitle.textContent = title;
    els.errorText.textContent = text;
    els.error.hidden = false;
    els.results.hidden = true;
  }

  /* ---------- rendering ---------- */
  function countUp(node, target) {
    if (reduceMotion) { node.textContent = target.toFixed(1); return; }
    const duration = 1200;
    const t0 = performance.now();
    const tick = (now) => {
      const p = Math.min(1, (now - t0) / duration);
      node.textContent = (target * (1 - Math.pow(1 - p, 3))).toFixed(1);
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  function fillList(ul, items, emptyText) {
    ul.replaceChildren(...(items.length ? items.map((t) => el('li', { text: t })) : [el('li', { class: 'empty', text: emptyText })]));
  }
  const AUTH_ICONS = {
    clean: '<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6l8-3z"/><path d="M8.5 12.2l2.4 2.4 4.6-5"/>',
    flag: '<path d="M12 3l9.5 17h-19L12 3z"/><path d="M12 10v4.5M12 17.6v.1"/>',
  };

  function renderResult(r) {
    const score = Number(r.worth_it_score) || 1;
    const aiFallback = (r.data_notes || []).some((note) => note.startsWith('AI analysis unavailable:'));
    const tier = tierOf(score);
    els.results.dataset.tier = tier;

    $('#type-chip').textContent = TYPE_LABEL[r.entity_type] || r.entity_type;
    $('#cached-chip').hidden = !r.cached;
    $('#entity-name').textContent = r.entity_name;
    $('#entity-sub').textContent = r.resolved_subtitle;
    $('#verdict-headline').textContent = r.verdict_headline;
    $('#rationale').textContent = r.rationale;
    const badge = $('#trust-badge');
    badge.dataset.level = r.trust_confidence;
    $('#trust-text').textContent = `${r.trust_confidence} trust`;

    const pills = Object.entries(r.source_breakdown || {}).map(([label, n]) =>
      el('span', { class: 'pill' + (/spam|low-quality/i.test(label) ? ' spam' : ''), title: `${n} ${label}` }, [el('b', { text: String(n) }), label]));
    $('#source-pills').replaceChildren(...pills);

    const notes = $('#notes');
    notes.replaceChildren(...(r.data_notes || []).map((n) => el('li', { text: n })));
    notes.hidden = !(r.data_notes || []).length;

    fillList($('#pros-list'), r.pros || [], aiFallback ? 'Not assessed in this rating-only result.' : 'No clear strengths surfaced.');
    fillList($('#cons-list'), r.cons || [], aiFallback ? 'Not assessed in this rating-only result.' : 'No recurring complaints surfaced.');

    const flags = r.red_flags || [];
    const auth = $('#auth-card');
    auth.dataset.clean = aiFallback ? 'unknown' : String(!flags.length);
    $('.auth-ico', auth).innerHTML = aiFallback ? '' : flags.length ? AUTH_ICONS.flag : AUTH_ICONS.clean;
    $('#auth-title').textContent = aiFallback ? 'Authenticity not assessed' : flags.length ? `Authenticity red flags (${flags.length})` : 'Clean authenticity signal';
    $('#auth-clean').textContent = aiFallback
      ? 'The AI service was unavailable, so this result could not assess fake-review patterns.'
      : 'No fake-review patterns, rating spikes or recent quality decline were detected in the sampled reviews.';
    fillList($('#flags-list'), flags, '');

    // reset filter, reveal, then animate the gauge
    applyFilter('all');
    els.results.hidden = false;
    selectTab(els.insightTabs, $('button[data-filter="all"]', els.insightTabs));
    $$('.reveal', els.results).forEach((node, i) => {
      node.classList.remove('enter');
      node.style.setProperty('--i', i);
      void node.offsetWidth;
      node.classList.add('enter');
    });
    const arc = $('#gauge-arc');
    arc.style.transition = 'none';
    arc.style.strokeDashoffset = '100';
    void arc.getBoundingClientRect();
    arc.style.transition = '';
    requestAnimationFrame(() => { arc.style.strokeDashoffset = String(100 - score * 10); });
    countUp($('#score-num'), score);
    els.results.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
  }

  function applyFilter(filter) {
    $$('.insights .card').forEach((card) => { card.hidden = !(filter === 'all' || card.dataset.group === filter); });
  }
  $$('button', els.insightTabs).forEach((b) => b.addEventListener('click', () => {
    selectTab(els.insightTabs, b);
    applyFilter(b.dataset.filter);
  }));

  /* ---------- actions ---------- */
  $('#reanalyze-btn').addEventListener('click', () => {
    if (state.lastRequest) { setType(state.lastRequest.type); els.input.value = state.lastRequest.query; }
    analyze({ force: true });
  });
  $('#copy-btn').addEventListener('click', async () => {
    const r = state.last;
    if (!r) return;
    const lines = [
      `${r.entity_name}: ${Number(r.worth_it_score).toFixed(1)}/10 (${r.trust_confidence} trust confidence)`,
      r.verdict_headline, '', r.rationale,
    ];
    if (r.pros.length) lines.push('', 'Pros:', ...r.pros.map((p) => `- ${p}`));
    if (r.cons.length) lines.push('', 'Cons:', ...r.cons.map((c) => `- ${c}`));
    const aiFallback = (r.data_notes || []).some((note) => note.startsWith('AI analysis unavailable:'));
    lines.push('', aiFallback ? 'Authenticity: not assessed (AI unavailable)' : r.red_flags.length ? ['Red flags:', ...r.red_flags.map((f) => `- ${f}`)].join('\n') : 'Red flags: none detected');
    const text = lines.join('\n');
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = el('textarea', { value: text });
      ta.style.cssText = 'position:fixed;opacity:0;';
      document.body.append(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (err) { toast('Copy failed. Select the text manually.'); ta.remove(); return; }
      ta.remove();
    }
    toast('Verdict copied');
  });
  els.retry.addEventListener('click', () => analyze());
  els.form.addEventListener('submit', (e) => { e.preventDefault(); analyze(); });

  /* ---------- init ---------- */
  setType('place');
  loadHealth();
  loadRecent();
  setInterval(loadHealth, 60000);
  requestAnimationFrame(() => allSegmented().forEach(movePill));
})();
