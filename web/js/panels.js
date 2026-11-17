/* Panels. Every number shown comes from /api/live (and its siblings) or the live event stream; a missing one says 'no data'. */
(function () {
  'use strict';
  const D = window.D;
  const P = (D.panels = {});

  /* ---------------------------------- ledger ---------------------------------- */
  const odo = {};
  P.ledger = (live) => {
    for (const s of live.strategies) {
      const id = s.name;
      odo[id] = odo[id] || new D.Odometer(D.$('#bal-' + id), (n) => D.usd(n));
      odo[id].set(s.balance);
      const pnl = D.$('#pnl-' + id);
      if (s.pnl_usd == null) { pnl.className = 'lane__pnl flat'; pnl.textContent = 'no data'; }
      else {
        const dir = D.dir(s.pnl_usd);
        pnl.className = 'lane__pnl ' + dir;
        pnl.replaceChildren(D.h('span', { 'aria-hidden': 'true', text: D.arrow(s.pnl_usd) }), D.usd(s.pnl_usd, { sign: true }), D.h('span', { text: '(' + D.pct(s.pnl_pct, { sign: true }) + ')' }));
      }
      const dl = D.$('#stats-' + id);
      const kv = [['Trades', s.trades], ['Win rate', s.win_rate == null ? 'no trades' : D.pct(s.win_rate, { d: 0 })], ['Last hour', s.trades_last_hour + ' trades'], ['Open', s.open.length], ['Fees', s.trades ? D.usd(s.fees_usd) : 'none yet']];
      dl.replaceChildren(...kv.map(([k, v]) => D.h('div', null, D.h('dt', { text: k }), D.h('dd', { text: String(v) }))));
    }
    D.$('#chart-legend').replaceChildren(...live.strategies.map((s) => D.h('span', null, D.h('i', { class: 'swatch swatch--' + (s.name === 'prompt' ? 'prompt' : 'orion') }), s.name + ' ', D.h('b', { text: D.usd(s.balance) }))));
  };

  /* ---------------------------------- header ---------------------------------- */
  P.header = (live) => {
    const pill = D.$('#mode-pill');
    pill.textContent = live.mode; pill.className = 'pill ' + (live.mode === 'LIVE' ? 'pill--live' : 'pill--paper');
    const f = live.feed;
    D.$('#feed-badge').textContent = f ? f.badge : 'no feed';
    const dot = D.$('#feed-dot');
    dot.className = 'dot ' + (!f ? 'dot--bad' : f.badge === 'PumpPortal live' ? 'dot--ok' : f.badge === 'feed down' ? 'dot--bad' : 'dot--warn');
  };

  /* ---------------------------------- agents ---------------------------------- */
  const agentEls = {};
  P.agents = (live) => {
    const list = D.$('#agents');
    const running = live.agents.filter((a) => a.running).length;
    D.$('#agents-title').textContent = 'Agents · ' + running + ' running';
    for (const a of live.agents) {
      let el = agentEls[a.name];
      const c = D.agentColor(a.name);
      if (!el) {
        el = agentEls[a.name] = { li: D.h('li', { class: 'agent', style: '--c:' + c }), now: D.h('p', { class: 'agent__now' }), dot: D.h('i', { class: 'dot' }), side: D.h('div', { class: 'agent__side' }), prev: null };
        const main = D.h('div', { class: 'agent__main' }, D.h('div', { class: 'agent__name' }, el.dot, D.AGENTS[a.name].label), el.now);
        el.li.append(D.h('i', { class: 'agent__tick' }), main, el.side);
        list.append(el.li);
      }
      el.li.style.setProperty('--c', c);
      if (el.prev != null && a.heartbeat_age_s != null && a.heartbeat_age_s + 0.4 < el.prev) { el.dot.classList.remove('is-beat'); void el.dot.offsetWidth; el.dot.classList.add('is-beat'); }
      el.prev = a.heartbeat_age_s;
      el.dot.style.opacity = a.running ? 1 : 0.35;
      const l = a.last;
      el.now.replaceChildren(...(a.paused ? ['paused by you'] : !a.running ? ['not running'] : l ? [D.h('b', { text: l.symbol || (l.mint || '').slice(0, 6) || 'desk' }), ' · ' + l.reason] : ['idle, nothing to judge yet']));
      el.side.replaceChildren(D.spark(a.rate, c), D.ring(a.pass_rate, c),
        D.h('button', { class: 'pause-btn', type: 'button', 'aria-pressed': a.paused ? 'true' : 'false', 'aria-label': (a.paused ? 'Resume ' : 'Pause ') + D.AGENTS[a.name].label, text: a.paused ? 'Resume' : 'Pause',
          onclick: async () => { try { await D.json('/api/bots/' + a.name + '/' + (a.paused ? 'resume' : 'pause'), { method: 'POST' }); D.emit('refresh'); } catch (e) { D.toast(e.message, 'bad'); } } }));
    }
  };

  /* ---------------------------------- decision log ---------------------------------- */
  const LOG = { rows: [], strategy: 'all', agents: new Set(), max: 300 };
  const decisionClass = (d) => (['pass', 'buy', 'size'].includes(d) ? 'reason--ok' : ['kill', 'reject', 'skip', 'drop'].includes(d) ? 'reason--no' : d === 'wait' ? 'reason--wait' : '');
  const logRow = (r, isNew) => {
    const c = D.agentColor(r.bot);
    const li = D.h('li', { style: '--c:' + c, class: isNew ? 'is-new' : '' },
      D.h('time', { text: D.clock(r.ts) }), D.h('span', { class: 'tag', text: (D.AGENTS[r.bot] || { label: r.bot }).label }),
      D.h('div', { class: 'log__body' },
        r.mint ? D.h('button', { class: 'coin-link', type: 'button', text: r.symbol || r.mint.slice(0, 6), onclick: () => D.emit('open-coin', r.mint, r.symbol) }) : D.h('span', { class: 'log__sym', text: 'desk' }),
        D.h('span', { class: 'reason ' + decisionClass(r.decision), text: r.reason || r.decision }), r.value ? D.h('span', { class: 'meta', text: r.value }) : null));
    return li;
  };
  const passes = (r) => (LOG.strategy === 'all' || r.strategy === LOG.strategy) && (LOG.agents.size === 0 || LOG.agents.has(r.bot));
  const renderLog = () => {
    const ol = D.$('#log'), rows = LOG.rows.filter(passes).slice(0, 200);
    ol.replaceChildren(...rows.map((r) => logRow(r, false)));
    D.$('#log-empty').hidden = rows.length > 0;
    D.$('#log-count').textContent = rows.length ? rows.length + ' shown' : '';
  };
  P.logFilters = () => {
    const box = D.$('#log-filters');
    const chip = (label, active, onclick, color) => D.h('button', { class: 'chip', type: 'button', 'aria-pressed': active ? 'true' : 'false', style: color ? '--c:' + color : '', onclick }, color ? D.h('i') : null, label);
    const draw = () => box.replaceChildren(
      ...[['all', 'Both strategies'], ['prompt', 'prompt'], ['orion_fast', 'orion_fast']].map(([k, label]) => chip(label, LOG.strategy === k, () => { LOG.strategy = k; draw(); renderLog(); })),
      ...Object.keys(D.AGENTS).map((k) => chip(D.AGENTS[k].label, LOG.agents.has(k), () => { LOG.agents.has(k) ? LOG.agents.delete(k) : LOG.agents.add(k); draw(); renderLog(); }, D.agentColor(k))));
    draw();
  };
  P.log = (live) => {
    if (!LOG.rows.length) { LOG.rows = live.decisions.slice(); renderLog(); }
  };
  P.logEvent = (d) => {
    LOG.rows.unshift(d); if (LOG.rows.length > LOG.max) LOG.rows.length = LOG.max;
    if (!passes(d)) return;
    const ol = D.$('#log'); ol.prepend(logRow(d, true));
    while (ol.children.length > 200) ol.lastChild.remove();
    D.$('#log-empty').hidden = true;
  };

  /* ---------------------------------- positions ---------------------------------- */
  let posTab = 'open', closedCache = null;
  P.positions = (live) => {
    const box = D.$('#positions'), empty = D.$('#pos-empty');
    if (posTab === 'open') {
      const rows = live.strategies.flatMap((s) => s.open.map((p) => ({ ...p, strategy: s.name })));
      box.replaceChildren(...rows.map((p) => D.h('div', { class: 'row' },
        D.h('button', { class: 'row__coin', type: 'button', onclick: () => D.emit('open-coin', p.mint, p.symbol) }, D.h('i', { class: 'swatch swatch--' + (p.strategy === 'prompt' ? 'prompt' : 'orion') }), D.h('span', null, p.symbol || p.mint.slice(0, 6), D.h('small', { text: p.strategy + ' · ' + D.ago(p.opened_at) }))),
        D.h('span', { class: 'row__num row__num--size', text: D.usd(p.size_usd) }),
        D.h('span', { class: 'row__num ' + D.dir(p.pnl_mark_usd) }, p.pnl_mark_usd == null ? 'no live price' : [D.arrow(p.pnl_mark_usd) + ' ' + D.usd(p.pnl_mark_usd, { sign: true }), D.h('small', { class: 'row__why', style: 'display:block', text: D.pct(p.pnl_mark_pct, { sign: true }) })]),
        D.h('button', { class: 'mini-btn', type: 'button', text: 'Open', onclick: () => D.emit('open-coin', p.mint, p.symbol) }))));
      empty.hidden = rows.length > 0; empty.textContent = 'No open positions. They appear here when the Executor or orion buys.';
    } else if (closedCache) {
      box.replaceChildren(...closedCache.map((t) => D.h('div', { class: 'row' },
        D.h('button', { class: 'row__coin', type: 'button', onclick: () => D.emit('open-coin', t.mint, t.symbol) }, D.h('i', { class: 'swatch swatch--' + (t.strategy === 'prompt' ? 'prompt' : 'orion') }), D.h('span', null, t.symbol || t.mint.slice(0, 6), D.h('small', { text: t.strategy + ' · ' + D.ago(t.closed_at) + ' ago' }))),
        D.h('span', { class: 'row__num row__num--size', text: D.usd(t.size_usd) }),
        D.h('span', { class: 'row__num ' + D.dir(t.pnl_usd) }, D.arrow(t.pnl_usd) + ' ' + D.usd(t.pnl_usd, { sign: true }), D.h('small', { class: 'row__why', style: 'display:block', text: D.pct(t.pnl_pct, { sign: true }) })),
        D.h('span', { class: 'row__why', text: t.exit_reason || '' }))));
      empty.hidden = closedCache.length > 0; empty.textContent = 'No closed trades yet';
    }
  };
  P.loadClosed = async () => { try { closedCache = (await D.json('/api/activity?limit=30')).closed; P.positions(D.state.live); } catch (e) { if (!(e instanceof D.AuthError)) console.error(e); } };
  P.posTab = (t) => { posTab = t; D.$$('#pos-tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.pos === t))); if (t === 'closed') P.loadClosed(); else if (D.state.live) P.positions(D.state.live); };

  /* ---------------------------------- audit discs ---------------------------------- */
  const AUD = { mint: null, symbol: null, layers: Array(8).fill(null), score: null };
  const C = 2 * Math.PI * 17;
  const disc = (i, L) => {
    const state = L == null ? 'off' : L.p === 1 ? 'ok' : L.p === 0 ? 'bad' : 'off';
    const col = state === 'ok' ? 'var(--green-hi)' : state === 'bad' ? 'var(--red-hi)' : 'var(--muted)';
    const svg = D.svg('svg', { viewBox: '0 0 46 46', 'aria-hidden': 'true' }, D.svg('circle', { class: 'disc__track', cx: 23, cy: 23, r: 17 }),
      D.svg('circle', { class: 'disc__ring', cx: 23, cy: 23, r: 17, stroke: col, 'stroke-dasharray': C, 'stroke-dashoffset': state === 'off' ? C : 0 }));
    if (state === 'ok') svg.append(D.svg('path', { d: 'M15.5 23.5l5 5 10-11', fill: 'none', stroke: col, 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    if (state === 'bad') svg.append(D.svg('path', { d: 'M17 17l12 12M29 17L17 29', fill: 'none', stroke: col, 'stroke-width': 3, 'stroke-linecap': 'round' }));
    return D.h('div', { class: 'disc is-' + state }, svg, D.h('span', { class: 'disc__name', text: D.LAYERS[i] }), D.h('span', { class: 'disc__val', text: L ? (L.value_text || L.value || '') : 'not checked' }));
  };
  const drawAudit = (box) => {
    const parts = AUD.layers.map((L, i) => disc(i, L));
    const sc = AUD.score;
    const score = sc ? D.h('div', { class: 'score' }, D.h('b', { text: String(Math.round(sc.score)) }), D.h('span', null, 'score · ' + (sc.p === 1 ? 'passed' : 'below the line')), D.h('div', { class: 'score__bar' }, D.h('i', { style: 'width:' + Math.min(100, sc.score) + '%' })),
      D.h('span', { class: 'meta', text: sc.reason || '' })) : null;
    box.replaceChildren(...parts);
    if (score) box.parentElement === document.body ? null : box.insertAdjacentElement('afterend', score);
  };
  const renderAudit = () => {
    const box = D.$('#audit'), panel = box.closest('.panel');
    const old = panel.querySelector('.score'); if (old) old.remove();
    D.$('#audit-title').textContent = AUD.mint ? (AUD.symbol || AUD.mint.slice(0, 8)) : 'pick a coin';
    D.$('#audit-empty').hidden = !!AUD.mint;
    box.hidden = !AUD.mint;
    if (AUD.mint) drawAudit(box);
  };
  P.auditLoad = async (mint, symbol) => {
    try {
      const j = await D.json('/api/audit/' + encodeURIComponent(mint));
      AUD.mint = mint; AUD.symbol = symbol || AUD.symbol; AUD.layers = Array(8).fill(null);
      for (const L of j.layers) AUD.layers[L.layer - 1] = L;
      AUD.score = j.score; renderAudit();
    } catch (e) { if (!(e instanceof D.AuthError)) console.error(e); }
  };
  P.auditEvent = (d) => {
    if (d.layer === 1 || d.mint !== AUD.mint) { AUD.mint = d.mint; AUD.layers = Array(8).fill(null); AUD.score = null; AUD.symbol = (D.symbols && D.symbols[d.mint]) || null; }
    AUD.layers[d.layer - 1] = { layer: d.layer, value_text: d.value, p: d.pass };
    renderAudit();
  };
  P.auditDone = (d) => { if (d.mint === AUD.mint) { AUD.symbol = d.symbol || AUD.symbol; setTimeout(() => P.auditLoad(d.mint, d.symbol), 400); } };
  P.auditFromLive = (live) => {
    if (AUD.mint || !live.audit) return;   // open on the most recently audited coin
    P.auditLoad(live.audit.mint, live.audit.symbol || (D.symbols && D.symbols[live.audit.mint]));
  };

  /* ---------------------------------- radar / kills / speed / health / recap ---------------------------------- */
  P.radar = (live) => { D.radar.set(live.radar); D.$('#radar-empty').hidden = live.radar.length > 0; D.$('#radar-wrap').style.display = live.radar.length ? '' : 'none'; };
  P.kills = (live) => {
    const ul = D.$('#kills'), max = Math.max(1, ...live.kills.map((k) => k.count));
    D.$('#kills-empty').hidden = live.kills.length > 0;
    const existing = new Map(D.$$('.bar', ul).map((b) => [b.dataset.k, b]));
    const keep = new Set();
    for (const k of live.kills) {
      const key = k.bot + '/' + k.bucket; keep.add(key);
      let li = existing.get(key);
      if (!li) { li = D.h('li', { class: 'bar', 'data-k': key }, D.h('span', { class: 'bar__label' }), D.h('div', { class: 'bar__track' }, D.h('div', { class: 'bar__fill' })), D.h('span', { class: 'bar__n' })); ul.append(li); requestAnimationFrame(() => requestAnimationFrame(() => { li.querySelector('.bar__fill').style.width = (100 * k.count / max) + '%'; })); }
      li.style.setProperty('--c', D.agentColor(k.bot));
      li.querySelector('.bar__label').textContent = k.bucket; li.querySelector('.bar__label').title = (D.AGENTS[k.bot] || {}).label + ': ' + k.bucket;
      li.querySelector('.bar__n').textContent = k.count; li.querySelector('.bar__fill').style.width = (100 * k.count / max) + '%';
    }
    existing.forEach((li, key) => { if (!keep.has(key)) li.remove(); });
    ul.append(...D.$$('.bar', ul).sort((a, b) => Number(b.querySelector('.bar__n').textContent) - Number(a.querySelector('.bar__n').textContent)));
  };
  P.speed = (live) => {
    const box = D.$('#speed'), st = live.stages || {}, names = Object.keys(st).filter((k) => st[k].n).sort();
    D.$('#speed-empty').hidden = names.length > 0;
    const p95 = live.loop_lag_ms && live.loop_lag_ms.p95, chip = D.$('#lag-chip');
    chip.textContent = p95 == null ? 'loop lag: no data' : 'loop lag p95 ' + D.ms(p95);
    chip.className = 'meta ' + (p95 == null ? '' : p95 < 100 ? 'up' : 'down');
    const max = Math.max(1, ...names.map((k) => st[k].p95 || 0));
    box.replaceChildren(...names.map((k) => D.h('div', { class: 'speed__row' }, D.h('span', { text: k }),
      D.h('div', { class: 'speed__track' }, D.h('i', { class: 'speed__p95', style: 'width:' + (100 * (st[k].p95 || 0) / max) + '%' }), D.h('i', { class: 'speed__p50', style: 'width:' + (100 * (st[k].p50 || 0) / max) + '%' })),
      D.h('span', { class: 'speed__nums', text: (D.isNum(st[k].p50) ? Math.round(st[k].p50 * 10) / 10 : '?') + ' / ' + (D.isNum(st[k].p95) ? Math.round(st[k].p95 * 10) / 10 : '?') + ' ms' }))));
  };
  P.health = (live) => {
    const ul = D.$('#health'), bots = Object.entries(live.bots).sort();
    D.$('#health-meta').textContent = live.restarts.length ? live.restarts.length + ' restart' + (live.restarts.length > 1 ? 's' : '') : 'no restarts';
    ul.replaceChildren(...bots.map(([k, age]) => D.h('li', null, D.h('i', { class: 'dot ' + (age < 30 ? 'dot--ok' : age < 95 ? 'dot--warn' : 'dot--bad'), style: age < 30 ? 'animation:none' : '' }), k, D.h('span', { class: 'health__age', text: D.dur(age) + ' ago' }))),
      ...live.restarts.slice(-3).map((r) => D.h('li', null, D.h('i', { class: 'dot dot--warn' }), 'restarted ' + r.bot, D.h('span', { class: 'health__age', text: r.reason }))));
    if (!bots.length) ul.append(D.h('li', null, D.h('span', { class: 'empty', text: 'No agent has reported yet' })));
  };
  P.recap = (live) => {
    const n = live.night, dl = D.$('#recap');
    D.$('#recap-empty').hidden = !!n; dl.hidden = !n;
    if (!n) return;
    D.$('#recap-meta').textContent = 'since ' + D.hm(n.started_at);
    const rows = [['Trades', n.trades], ['Coins passed', n.passes], ['Coins killed', n.kills], ['Buys', n.buys], ['Sells', n.sells], ['Started with', D.usd(n.start_equity)]];
    dl.replaceChildren(...rows.map(([k, v]) => D.h('div', null, D.h('dt', { text: k }), D.h('dd', { text: String(v) }))));
  };
  P.lane = (live) => { D.$('#frame-meta').textContent = 'holding the lane: ' + ((live.lane && live.lane.holder) || 'nobody'); };

  /* ---------------------------------- account + settings ---------------------------------- */
  P.account = async () => {
    try {
      const [acc, mode] = await Promise.all([D.json('/api/account'), D.json('/api/mode')]);
      D.$('#account').replaceChildren(
        D.h('dl', { class: 'kv' }, D.h('dt', { text: 'FOMO handle' }), D.h('dd', { text: acc.handle || 'not proven yet' }), D.h('dt', { text: 'Wallet' }), D.h('dd', { text: acc.address || 'not proven yet' }), D.h('dt', { text: 'Mode' }), D.h('dd', { text: mode.mode })),
        D.h('p', { class: 'meta', style: 'margin:12px 0 2px', text: 'LIVE stays locked until every gate is met, and then only you can unlock it:' }),
        ...mode.gates.map((g) => D.h('p', { class: 'gate' }, D.h('span', { class: g.met ? 'up' : 'down', text: g.met ? '✓' : '✗' }), g.gate)),
        D.h('button', { class: 'btn', type: 'button', style: 'margin-top:14px', text: 'Switch dark and light theme', onclick: () => D.theme.toggle() }));
    } catch (e) { if (!(e instanceof D.AuthError)) D.$('#account').textContent = 'Could not load the account: ' + e.message; }
  };
  const GROUPS = ['session', 'scanner', 'analyst', 'brain', 'executor', 'risk', 'fees', 'telegram'];
  const GROUP_NAME = { session: 'Session and cash', scanner: 'Scanner', analyst: 'Analyst', brain: 'Brain', executor: 'Executor', risk: 'Risk', fees: 'Paper fees', telegram: 'Telegram' };
  P.settings = async () => {
    try {
      const { settings } = await D.json('/api/settings');
      const open = new Set(D.$$('#settings details[open]').map((d) => d.dataset.g));
      D.$('#settings').replaceChildren(...GROUPS.map((g) => {
        const rows = settings.filter((s) => s.group === g);
        const det = D.h('details', { class: 'set-group', 'data-g': g }, D.h('summary', null, GROUP_NAME[g]));
        if (open.has(g)) det.open = true;
        rows.forEach((s) => det.append(settingRow(s)));
        return det;
      }));
    } catch (e) { if (!(e instanceof D.AuthError)) D.$('#settings').textContent = 'Could not load the settings: ' + e.message; }
  };
  function settingRow(s) {
    const err = D.h('p', { class: 'set__err', hidden: true });
    const row = D.h('div', { class: 'set' + (String(s.value) !== String(s.default) ? ' is-changed' : '') });
    const save = async (val, revert) => {
      try { await D.json('/api/settings', { method: 'POST', body: { key: s.key, value: val } }); err.hidden = true; s.value = val; row.classList.toggle('is-changed', String(val) !== String(s.default)); D.toast('Saved ' + s.label, 'ok'); }
      catch (e) { err.textContent = e.message; err.hidden = false; revert(); }
    };
    row.append(D.h('div', { class: 'set__label' }, s.label, D.h('small', { text: 'default ' + s.default + (s.lo != null ? ' · allowed ' + s.lo + ' to ' + s.hi : '') })));
    if (s.kind === 'bool') {
      const cb = D.h('input', { type: 'checkbox', 'aria-label': s.label }); cb.checked = !!s.value;
      cb.addEventListener('change', () => save(cb.checked, () => { cb.checked = !!s.value; }));
      row.append(D.h('span'), D.h('span', { style: 'text-align:right' }, cb));
    } else if (s.kind === 'time') {
      const t = D.h('input', { type: 'text', value: s.value, inputmode: 'numeric', 'aria-label': s.label, pattern: '[0-2][0-9]:[0-5][0-9]' });
      t.addEventListener('change', () => save(t.value, () => { t.value = s.value; }));
      row.append(D.h('span'), t);
    } else {
      const step = s.hi != null ? (s.hi - s.lo > 200 ? Math.max(1, Math.round((s.hi - s.lo) / 400)) : s.hi - s.lo > 20 ? 1 : 0.05) : 1;
      const range = D.h('input', { type: 'range', min: s.lo, max: s.hi, step, value: s.value, 'aria-label': s.label + ' slider' });
      const num = D.h('input', { type: 'number', value: s.value, min: s.lo, max: s.hi, step: 'any', inputmode: 'decimal', 'aria-label': s.label });
      range.addEventListener('input', () => { num.value = range.value; });
      range.addEventListener('change', () => save(Number(range.value), () => { range.value = s.value; num.value = s.value; }));
      num.addEventListener('change', () => { range.value = num.value; save(Number(num.value), () => { range.value = s.value; num.value = s.value; }); });
      row.append(range, num);
    }
    row.append(err);
    return row;
  }

  /* ---------------------------------- live Chrome frame (double-buffered) ---------------------------------- */
  let front = 'a', frameBusy = false;
  P.frame = async () => {
    const wrap = D.$('#frame-wrap');
    if (!wrap.offsetParent || document.hidden || frameBusy) return;
    frameBusy = true;
    const empty = D.$('#frame-empty');
    try {
      const r = await D.fetch('/api/screen.jpg');
      if (!r.ok || !(r.headers.get('content-type') || '').startsWith('image/')) {   // a status message instead of a frame
        let j = null; try { j = await r.json(); } catch (e) { /* not json */ }
        empty.hidden = false; empty.textContent = (j && j.message) || 'Waiting for the desk Chrome'; wrap.classList.remove('has-frame'); return;
      }
      const url = URL.createObjectURL(await r.blob());
      const back = D.$('#frame-' + (front === 'a' ? 'b' : 'a')), cur = D.$('#frame-' + front);
      await new Promise((res) => { back.onload = res; back.onerror = res; back.src = url; });
      back.classList.add('is-on'); cur.classList.remove('is-on');
      if (cur.src.startsWith('blob:')) URL.revokeObjectURL(cur.src);
      front = front === 'a' ? 'b' : 'a'; empty.hidden = true; wrap.classList.add('has-frame');
    } catch (e) { if (!(e instanceof D.AuthError)) empty.hidden = false; } finally { frameBusy = false; }
  };

  /* ---------------------------------- coin sheet ---------------------------------- */
  P.openCoin = async (mint, symbolHint) => {
    const dlg = D.$('#coin'), note = D.$('#coin-note');
    note.textContent = ''; note.className = 'sheet__note';
    D.$('#coin-title').textContent = symbolHint || mint.slice(0, 8);
    D.$('#coin-sub').textContent = 'loading';
    D.$('#coin-audit').replaceChildren(); D.$('#coin-actions').replaceChildren();
    if (!dlg.open) dlg.showModal();
    try {
      const [coin, audit] = await Promise.all([D.json('/api/coin/' + encodeURIComponent(mint)), D.json('/api/audit/' + encodeURIComponent(mint))]);
      const c = coin.coin || {}, pos = coin.position;
      D.$('#coin-title').textContent = (c.symbol || symbolHint || mint.slice(0, 8)) + (c.name ? ' · ' + c.name : '');
      D.$('#coin-sub').textContent = pos ? 'open in ' + pos.strategy + ' since ' + D.hm(pos.opened_at) : coin.scan ? 'last scan: ' + coin.scan.verdict + ' · ' + (coin.scan.reason || '') : 'no open position';
      const ok = D.drawCandles(D.$('#candles'), coin.candles, pos);
      D.$('#candles-empty').hidden = ok;
      const layers = Array(8).fill(null); audit.layers.forEach((L) => (layers[L.layer - 1] = L));
      D.$('#coin-audit').replaceChildren(...layers.map((L, i) => disc(i, L)));
      const acts = D.$('#coin-actions');
      acts.append(D.h('a', { class: 'link-btn', href: coin.fomo_url, target: '_blank', rel: 'noopener noreferrer', text: 'Open on FOMO' }));
      if (pos) {
        let pct = 50, armed = false;
        const seg = D.h('div', { class: 'seg', role: 'group', 'aria-label': 'How much to sell' });
        const go = D.h('button', { class: 'btn btn--danger', type: 'button', text: 'Sell now' });
        const draw = () => { seg.replaceChildren(...[25, 50, 100].map((p) => D.h('button', { type: 'button', 'aria-pressed': String(p === pct), text: p + '%', onclick: () => { pct = p; armed = false; draw(); } }))); go.textContent = armed ? 'Confirm: sell ' + pct + '% (paper)' : 'Sell now'; };
        go.addEventListener('click', async () => {
          if (!armed) { armed = true; draw(); note.textContent = 'Press again to confirm. This is a paper sale at the last real price.'; return; }
          try { const r = await D.json('/api/sell/' + encodeURIComponent(mint), { method: 'POST', body: { pct, confirm: true } }); note.className = 'sheet__note is-ok'; note.textContent = 'Sell queued: ' + r.pct + '% (paper). ' + r.note + '.'; armed = false; draw(); setTimeout(() => D.emit('refresh'), 1500); }
          catch (e) { note.className = 'sheet__note is-bad'; note.textContent = e.message; armed = false; draw(); }
        });
        draw(); acts.append(seg, go);
      }
    } catch (e) { if (!(e instanceof D.AuthError)) { D.$('#coin-sub').textContent = e.status === 404 ? 'the desk has not seen this coin' : e.message; } }
  };
})();
