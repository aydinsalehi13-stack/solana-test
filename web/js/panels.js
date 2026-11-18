/* Panels. Every number shown comes from /api/live (and its siblings) or the live event stream; a missing one says 'no data'. */
(function () {
  'use strict';
  const D = window.D;
  const P = (D.panels = {});

  /* ---------------------------------- hero + the strategies ----------------------------------
     The big number and the line show ONE thing you pick (default: prompt, which starts at $150): prompt, orion_fast, or both added up. */
  const odo = {};
  const VIEWS = ['prompt', 'orion_fast', 'both'];
  D.state.view = (() => { try { const v = localStorage.getItem('desk_view'); return VIEWS.includes(v) ? v : 'prompt'; } catch (e) { return 'prompt'; } })();
  const selected = (live) => (D.state.view === 'both' ? live.strategies : live.strategies.filter((s) => s.name === D.state.view));
  const sumOf = (rows, key) => (rows.length && rows.every((r) => D.isNum(r[key])) ? rows.reduce((a, r) => a + r[key], 0) : null);   // one missing value = no data, never a partial sum
  P.setView = (v) => {
    if (!VIEWS.includes(v) || v === D.state.view) return;
    D.state.view = v;
    try { localStorage.setItem('desk_view', v); } catch (e) { /* private mode */ }
    if (D.state.live) P.ledger(D.state.live);
    D.equity.refit();
  };
  P.ledger = (live) => {
    const rows = selected(live);
    const total = sumOf(rows, 'balance'), putIn = rows.reduce((a, s) => a + s.start_cash, 0);
    const trades = rows.reduce((a, s) => a + s.trades, 0), wins = rows.reduce((a, s) => a + s.wins, 0);
    P.totals = { total, putIn, trades, wins, losses: trades - wins };
    odo.total = odo.total || new D.Odometer(D.$('#bal-total'), (n) => D.usd(n));
    if (D.isNum(total) && D.isNum(P.lastTotal) && Math.abs(total - P.lastTotal) >= 0.005) {   // the number moved: a brief green or red tint
      const bal = D.$('.hero__bal'); bal.classList.remove('is-up', 'is-down'); void bal.offsetWidth; bal.classList.add(total > P.lastTotal ? 'is-up' : 'is-down');
    }
    P.lastTotal = total;
    odo.total.set(total);
    D.fitHero();
    D.$('#hero-view').textContent = D.state.view === 'both' ? 'both strategies' : D.state.view;
    const pnl = total == null ? null : total - putIn;
    const el = D.$('#hero-pnl');
    el.className = pnl == null ? 'flat' : D.dir(pnl);
    el.textContent = pnl == null ? 'no data yet' : D.usd(pnl, { sign: true }) + ' · ' + wins + 'W ' + (trades - wins) + 'L';
    D.$('#hero-in-text').textContent = total == null ? 'no balance yet' : 'in ' + D.usd(putIn, { d: putIn % 1 ? 2 : 0 }) + ' · ' + trades + (trades === 1 ? ' trade' : ' trades');
    const mult = D.$('#hero-mult');
    mult.hidden = total == null || !putIn;
    if (!mult.hidden) mult.textContent = (total / putIn).toFixed(2) + '×';
    const hour = sumOf(rows.map((s) => ({ v: live.delta_1h ? live.delta_1h[s.name] : null })), 'v');
    const delta = D.$('#hero-delta');
    delta.hidden = hour == null;
    if (!delta.hidden) {
      const txt = D.usd(hour, { sign: true, d: Math.abs(hour) >= 100 ? 0 : 2 });
      if (delta.textContent !== txt) { delta.textContent = txt; delta.className = 'delta ' + D.dir(hour); delta.style.animation = 'none'; void delta.offsetWidth; delta.style.animation = ''; }
      delta.title = 'Change in the last hour'; delta.setAttribute('aria-label', 'Change in the last hour: ' + txt);
    }
    const segs = (s) => (s.trades ? [s.trades + (s.trades === 1 ? ' trade' : ' trades'), D.pct(s.win_rate, { d: 0 }) + ' win', s.open.length + ' open', 'fees ' + D.usd(s.fees_usd)] : ['no trades yet', s.open.length + ' open']);
    const fill = (el, parts) => el.replaceChildren(...parts.flatMap((t, i) => (i ? [' · ', D.h('span', { class: 'seg-nb', text: t })] : [D.h('span', { class: 'seg-nb', text: t })])));   // the line can only break between two facts, never inside one
    for (const s of live.strategies) {
      const id = s.name;
      odo[id] = odo[id] || new D.Odometer(D.$('#bal-' + id), (n) => D.usd(n));
      odo[id].set(s.balance);
      const p = D.$('#pnl-' + id);
      if (s.pnl_usd == null) { p.className = 'strat__pnl flat'; p.textContent = 'no data'; }
      else { p.className = 'strat__pnl ' + D.dir(s.pnl_usd); p.textContent = D.usd(s.pnl_usd, { sign: true }) + ' (' + D.pct(s.pnl_pct, { sign: true }) + ')'; }
      fill(D.$('#stats-' + id), segs(s));
    }
    // "both": the two added up, and only when both have a balance
    const sum = sumOf(live.strategies, 'balance'), start = live.strategies.reduce((a, s) => a + s.start_cash, 0);
    D.$('#bal-both').textContent = sum == null ? 'no data' : D.usd(sum);
    const bp = D.$('#pnl-both');
    bp.className = 'strat__pnl ' + (sum == null ? 'flat' : D.dir(sum - start));
    bp.textContent = sum == null ? 'no data' : D.usd(sum - start, { sign: true }) + ' (' + D.pct(100 * (sum - start) / start, { sign: true }) + ')';
    const tr = live.strategies.reduce((a, s) => a + s.trades, 0), op = live.strategies.reduce((a, s) => a + s.open.length, 0);
    fill(D.$('#stats-both'), ['put in ' + D.usd(start, { d: 0 }), tr + (tr === 1 ? ' trade' : ' trades'), op + ' open']);
    D.$$('#strats .strat').forEach((e) => { const on = e.dataset.view === D.state.view; e.classList.toggle('is-on', on); e.setAttribute('aria-checked', String(on)); });
    const sw = (c) => D.h('i', { style: 'border-color:' + c });
    D.$('#chart-legend').replaceChildren(D.h('span', null, D.h('i'), 'value'), D.h('span', null, D.h('i', { class: 'dash' }), 'put in'),
      ...(D.state.view === 'both' ? live.strategies.map((s) => D.h('span', { class: 'legend__s' }, sw(D.css(s.name === 'prompt' ? '--prompt' : '--orion')), s.name)) : []));
  };
  D.$$('#strats .strat').forEach((e) => {
    e.addEventListener('click', () => P.setView(e.dataset.view));
    e.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); P.setView(e.dataset.view); } });
  });

  /* ---------------------------------- header + the meta line under it ---------------------------------- */
  const SHORT = { 'PumpPortal live': 'Live', 'DexScreener snapshots': 'Snapshots', 'feed down': 'Feed down' };
  P.header = (live) => {
    const pill = D.$('#mode-pill');
    pill.replaceChildren(...(live.mode === 'LIVE' ? ['LIVE'] : ['paper', D.h('span', { class: 'mode-word', text: ' mode' })])); pill.classList.toggle('is-live', live.mode === 'LIVE');   // a narrow screen shows just "paper"
    const f = live.feed, badge = f ? f.badge : 'no feed';
    D.$('#feed-badge').textContent = badge;
    D.$('#feed-short').textContent = !f ? 'No feed' : SHORT[badge] || 'Slow feed';
    D.$('#live-pill').title = 'Coins and prices are coming from: ' + badge;
    D.$('#feed-dot').className = 'dot ' + (!f ? 'dot--bad' : badge === 'PumpPortal live' ? 'dot--ok' : badge === 'feed down' ? 'dot--bad' : 'dot--warn');
    const running = live.agents.filter((a) => a.running).length;
    D.$('#hero-agents').textContent = running + (running === 1 ? ' agent' : ' agents');
    P.night = live.night;
    P.clock();
  };
  P.clock = () => {
    D.$('#hero-clock').textContent = D.hm(Date.now() / 1000);
    D.$('#hero-in').textContent = P.night ? D.durHM(Date.now() / 1000 - P.night.started_at) + ' in' : '';
  };

  /* ---------------------------------- the coin tape ---------------------------------- */
  let tickerKey = '';
  P.ticker = (live) => {
    const rows = live.ticker || [], track = D.$('#ticker-track');
    const key = rows.map((r) => r.mint + ':' + r.h1_change_pct).join('|');
    if (key === tickerKey) return;   // same coins, same numbers: leave the moving tape alone
    tickerKey = key;
    if (!rows.length) { track.className = 'ticker__track is-still'; track.replaceChildren(D.h('span', { class: 'tk tk--wait', text: 'waiting for the first scanned coin' })); return; }
    const chip = (r) => D.h('span', { class: 'tk', role: 'button', tabindex: '0', onclick: () => D.emit('open-coin', r.mint, r.symbol), onkeydown: (e) => { if (e.key === 'Enter') D.emit('open-coin', r.mint, r.symbol); } },
      D.avatar(r.mint, r.symbol), D.sym(r.symbol, r.mint), D.h('span', { class: D.dir(r.h1_change_pct), text: D.isNum(r.h1_change_pct) ? D.pct(r.h1_change_pct, { sign: true, d: 2 }) : 'no data' }), D.h('small', { text: '1h' }));
    const still = D.reduced() || rows.length < 4;
    track.className = 'ticker__track' + (still ? ' is-still' : '');
    track.style.setProperty('--dur', Math.max(24, rows.length * 4.5) + 's');
    track.replaceChildren(...rows.map(chip), ...(still ? [] : rows.map(chip)));   // the second copy makes the loop seamless
  };

  /* ---------------------------------- agents ---------------------------------- */
  const agentEls = {};
  const STRAT_OF = (name) => (name === 'orion' ? 'orion_fast' : 'prompt');
  const beat = (el, age) => {   // a real heartbeat arrived (its age went back down): the robot's light blinks
    if (el.prev != null && age != null && age + 0.4 < el.prev) { el.li.classList.remove('is-beat'); void el.li.offsetWidth; el.li.classList.add('is-beat'); }
    el.prev = age;
  };
  const nod = (el) => { const t = performance.now(); if (t - (el.nodAt || 0) < 700) return; el.nodAt = t; el.li.classList.remove('is-nod'); void el.li.offsetWidth; el.li.classList.add('is-nod'); };
  P.agents = (live) => {
    const list = D.$('#agents');
    const running = live.agents.filter((a) => a.running).length;
    D.$('#agents-count').textContent = running + ' running';
    const st = D.$('#agents-run'); st.classList.toggle('is-off', running === 0); st.querySelector('.dot').className = 'dot ' + (running ? 'dot--ok' : '');
    for (const a of live.agents) {
      let el = agentEls[a.name];
      const c = D.agentColor(a.name);
      if (!el) {
        el = agentEls[a.name] = { li: D.h('li', { class: 'agent' }), now: D.h('span', { class: 'agent__now' }), more: D.h('div', { class: 'agent__more' }), prev: null, key: '' };
        const row = D.h('button', { class: 'agent__row', type: 'button', 'aria-expanded': 'false', 'aria-label': D.AGENTS[a.name].label + ': show its pace and controls',
          onclick: () => { const open = el.li.classList.toggle('is-open'); row.setAttribute('aria-expanded', String(open)); } },
          D.use('i-bot', 'bot'), D.h('span', { class: 'agent__name', text: D.AGENTS[a.name].label }), el.now, D.h('span', { class: 'agent__src', text: STRAT_OF(a.name) }));
        el.li.append(row, el.more);
        list.append(el.li);
      }
      el.li.style.setProperty('--c', c);
      el.li.classList.toggle('is-idle', !a.running);
      beat(el, a.heartbeat_age_s);
      const l = a.last;
      const key = (a.paused ? 'paused' : !a.running ? 'off' : l ? l.ts + '|' + l.reason : 'idle') + (a.name === 'scanner' ? '|' + a.per_hour : '');
      if (key !== el.key) {
        const rate = a.name === 'scanner' && a.per_hour ? a.per_hour.toLocaleString('en-US') + '/h → ' : '';   // decisions really made in the last hour
        el.now.replaceChildren(...(a.paused ? ['paused by you'] : !a.running ? ['not running'] : l ? [rate, l.mint ? D.avatar(l.mint, l.symbol) : null, ' ', D.h('b', { text: l.mint ? D.sym(l.symbol, l.mint) : 'desk' }), ' ' + l.reason] : ['idle, nothing to judge yet']));
        const t = performance.now();
        if (el.key && t - (el.swapAt || 0) > 450) { el.swapAt = t; el.now.classList.remove('is-swap'); void el.now.offsetWidth; el.now.classList.add('is-swap'); }
        el.key = key;
      }
      const tenMin = a.rate.reduce((x, y) => x + y, 0);
      el.more.replaceChildren(D.h('p', { class: 'agent__full', text: a.paused ? 'paused by you' : !a.running ? 'not running' : l ? (l.mint ? D.sym(l.symbol, l.mint) + ' · ' : '') + l.reason + ' · ' + D.ago(l.ts) + ' ago' : 'idle, nothing to judge yet' }),
        D.h('small', { text: tenMin + ' in 10 min' }), D.spark(a.rate, c), D.ring(a.pass_rate, c),
        D.h('button', { class: 'pause-btn', type: 'button', 'aria-pressed': a.paused ? 'true' : 'false', 'aria-label': (a.paused ? 'Resume ' : 'Pause ') + D.AGENTS[a.name].label, text: a.paused ? 'Resume' : 'Pause',
          onclick: async () => { try { await D.json('/api/bots/' + a.name + '/' + (a.paused ? 'resume' : 'pause'), { method: 'POST' }); D.emit('refresh'); } catch (e) { D.toast(e.message, 'bad'); } } }));
    }
  };

  /* ---------------------------------- decision log ---------------------------------- */
  const LOG = { rows: [], strategy: 'all', agents: new Set(), max: 300 };
  let lastPush = 0;
  const decisionClass = (d) => (['pass', 'buy', 'size'].includes(d) ? 'reason--ok' : ['kill', 'reject', 'skip', 'drop'].includes(d) ? 'reason--no' : d === 'wait' ? 'reason--wait' : '');
  const logRow = (r, isNew) => {
    const c = D.agentColor(r.bot), t = D.clock(r.ts);
    const open = () => { if (r.mint) D.emit('open-coin', r.mint, r.symbol); };
    return D.h('li', { style: '--c:' + c, class: isNew ? 'is-new' : '', tabindex: r.mint ? '0' : null, role: r.mint ? 'button' : null, onclick: open, onkeydown: (e) => { if (e.key === 'Enter') open(); } },
      D.h('time', null, t.slice(0, 5), D.h('span', { class: 'sec', text: t.slice(5) })), D.h('span', { class: 'tag', text: (D.AGENTS[r.bot] || { label: r.bot }).label }),
      D.h('div', { class: 'log__body' },
        r.mint ? D.avatar(r.mint, r.symbol) : null, D.h('span', { class: 'log__sym', text: r.mint ? D.sym(r.symbol, r.mint) : 'desk' }),
        D.h('span', { class: 'reason ' + decisionClass(r.decision), text: r.reason || r.decision }), r.value ? D.h('span', { class: 'log__val', text: r.value }) : null));
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
    const live = D.state.live, a = live && live.agents.find((x) => x.name === d.bot);
    if (a) {   // the agent's own row shows this decision at once: the robot nods, its line changes, its count goes up by one
      a.last = { ts: d.ts, bot: d.bot, decision: d.decision, mint: d.mint, symbol: d.symbol, reason: d.reason };
      a.per_hour = (a.per_hour || 0) + 1; a.rate[a.rate.length - 1]++;
      if (agentEls[a.name]) nod(agentEls[a.name]);
      clearTimeout(P.agentsSoon); P.agentsSoon = setTimeout(() => { if (D.state.live) P.agents(D.state.live); }, 60);
    }
    if (!passes(d)) return;
    const ol = D.$('#log'), row = logRow(d, true); ol.prepend(row);
    while (ol.children.length > 200) ol.lastChild.remove();
    ol.style.setProperty('--row', row.offsetHeight + 'px');   // the list slides down by exactly the height of the new line, however many lines it wraps to
    const now = performance.now();   // the push runs at most twice a second: a burst of decisions just lands, it never stacks animations
    if (now - lastPush > 500 && ol.scrollTop < 4 && !D.reduced()) { lastPush = now; ol.classList.remove('is-push'); void ol.offsetWidth; ol.classList.add('is-push'); }
    D.$('#log-empty').hidden = true;
  };
  P.stream = (ok) => {
    const st = D.$('#stream-status');
    st.className = 'status ' + (ok ? '' : 'is-warn'); st.querySelector('.dot').className = 'dot ' + (ok ? 'dot--ok' : 'dot--warn');
    D.$('#stream-label').textContent = ok ? 'streaming' : 'reconnecting';
  };

  /* ---------------------------------- positions ---------------------------------- */
  let posTab = 'open', closedCache = null;
  const seenRows = new Set();
  const rowClass = (key) => { const fresh = !seenRows.has(key); seenRows.add(key); return 'row' + (fresh ? ' is-new' : ''); };   // a row rises in once, when it first appears
  const pnlCell = (p) => (p.pnl_mark_usd == null ? ['no live price'] : [D.arrow(p.pnl_mark_usd) + ' ' + D.usd(p.pnl_mark_usd, { sign: true }), D.h('small', { class: 'row__why', style: 'display:block', text: D.pct(p.pnl_mark_pct, { sign: true }) })]);
  P.positions = (live) => {
    const box = D.$('#positions'), empty = D.$('#pos-empty');
    if (posTab === 'open') {
      const rows = live.strategies.flatMap((s) => s.open.map((p) => ({ ...p, strategy: s.name })));
      box.replaceChildren(...rows.map((p) => D.h('div', { class: rowClass('o' + p.mint + p.opened_at), 'data-mint': p.mint },
        D.h('button', { class: 'row__coin', type: 'button', onclick: () => D.emit('open-coin', p.mint, p.symbol) }, D.avatar(p.mint, p.symbol), D.h('span', null, D.sym(p.symbol, p.mint), D.h('small', { text: p.strategy + ' · ' + D.ago(p.opened_at) }))),
        D.h('span', { class: 'row__num row__num--size', text: D.usd(p.size_usd) }),
        D.h('span', { class: 'row__num row__pnl ' + D.dir(p.pnl_mark_usd) }, pnlCell(p)),
        D.h('button', { class: 'mini-btn', type: 'button', text: 'Open', onclick: () => D.emit('open-coin', p.mint, p.symbol) }))));
      empty.hidden = rows.length > 0; empty.textContent = 'No open positions. They appear here when the Executor or orion buys.';
    } else if (closedCache) {
      box.replaceChildren(...closedCache.map((t) => D.h('div', { class: rowClass('c' + t.mint + t.closed_at) },
        D.h('button', { class: 'row__coin', type: 'button', onclick: () => D.emit('open-coin', t.mint, t.symbol) }, D.avatar(t.mint, t.symbol), D.h('span', null, D.sym(t.symbol, t.mint), D.h('small', { text: t.strategy + ' · ' + D.ago(t.closed_at) + ' ago' }))),
        D.h('span', { class: 'row__num row__num--size', text: D.usd(t.size_usd) }),
        D.h('span', { class: 'row__num ' + D.dir(t.pnl_usd) }, D.arrow(t.pnl_usd) + ' ' + D.usd(t.pnl_usd, { sign: true }), D.h('small', { class: 'row__why', style: 'display:block', text: D.pct(t.pnl_pct, { sign: true }) })),
        D.h('span', { class: 'row__why', text: t.exit_reason || '' }))));
      empty.hidden = closedCache.length > 0; empty.textContent = 'No closed trades yet';
    }
  };

  /* ---------------------------------- the live tick (once a second, pushed by the desk) ----------------------------------
     Only what changed is touched: the big number, a strategy's line, a position's P&L cell, a robot's light. No reload, no re-render. */
  P.tick = (d) => {
    const live = D.state.live;
    if (!live) return;
    const now = Date.now() / 1000;
    if (d.equity) {
      let moved = false;
      for (const s of live.strategies) {
        const v = d.equity[s.name];
        if (!D.isNum(v) || (D.isNum(s.balance) && Math.abs(v - s.balance) < 0.005)) continue;
        s.balance = v; s.pnl_usd = v - s.start_cash; s.pnl_pct = s.start_cash ? (100 * (v - s.start_cash)) / s.start_cash : null; moved = true;
      }
      if (moved) { P.ledger(live); D.equity.live(live.strategies, now); }
    }
    if (d.marks) {
      for (const s of live.strategies) for (const p of s.open) {
        const m = d.marks[p.mint];
        if (!m || !D.isNum(m[0]) || !D.isNum(m[1])) continue;   // no live price: the last good one stays
        const pnl = m[1] * m[0] + (m[2] || 0) - p.size_usd;
        if (D.isNum(p.pnl_mark_usd) && Math.abs(pnl - p.pnl_mark_usd) < 0.005) continue;
        p.price = m[0]; p.pnl_mark_usd = pnl; p.pnl_mark_pct = p.size_usd ? (100 * pnl) / p.size_usd : null;
        const cell = posTab === 'open' && D.$$('#positions .row').find((r) => r.dataset.mint === p.mint);
        if (cell) { const c = cell.querySelector('.row__pnl'); c.className = 'row__num row__pnl ' + D.dir(pnl); c.replaceChildren(...pnlCell(p)); }
      }
    }
    if (d.ages) {
      let running = 0, flipped = false;
      for (const a of live.agents) {
        const age = d.ages[a.name], on = age != null && age < 95, paused = (d.paused || []).includes(a.name);
        if (on !== a.running || paused !== a.paused) flipped = true;
        a.heartbeat_age_s = age == null ? null : age; a.running = on; a.paused = paused;
        if (on) running++;
        if (agentEls[a.name]) beat(agentEls[a.name], a.heartbeat_age_s);
      }
      if (flipped) { P.agents(live); D.$('#hero-agents').textContent = running + (running === 1 ? ' agent' : ' agents'); }
    }
    if (d.feed !== undefined && (live.feed ? live.feed.badge : null) !== d.feed) { live.feed = d.feed ? { ...(live.feed || {}), badge: d.feed } : null; P.header(live); }
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
    ul.replaceChildren(...bots.map(([k, age]) => D.h('li', null, D.h('i', { class: 'dot dot--still ' + (age < 30 ? 'dot--ok' : age < 95 ? 'dot--warn' : 'dot--bad') }), k, D.h('span', { class: 'health__age', text: D.dur(age) + ' ago' }))),
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
  /* The night recap sheet. Every figure is read when it opens: the session from /api/live, the trades from /api/activity. */
  P.openRecap = async () => {
    const live = D.state.live, dlg = D.$('#recap-sheet');
    if (!live) return;
    const n = live.night, T = P.totals || {}, note = D.$('#rc-note');
    note.textContent = ''; note.className = 'sheet__note';
    const viewName = D.state.view === 'both' ? 'both strategies' : D.state.view;
    D.$('#rc-sub').textContent = viewName + ' · paper · ' + (n ? D.hm(n.started_at) + ' → ' + (n.ends_at ? D.hm(n.ends_at) : 'open') : 'no session yet');
    D.$('#rc-in').textContent = T.total == null ? 'no data' : D.usd(T.putIn);
    const now = D.$('#rc-now'); now.className = T.total == null ? 'flat' : D.dir(T.total - T.putIn) === 'down' ? 'down' : 'up';
    now.textContent = T.total == null ? 'no data' : D.usd(T.total); now.setAttribute('aria-label', now.textContent);
    if (T.total != null && !D.reduced()) {   // the figure travels from what was put in to what it is worth now, then rests on the exact value
      const from = T.putIn, to = T.total, t0 = performance.now();
      const step = (t) => { const k = Math.min(1, (t - t0) / 650), e = 1 - Math.pow(1 - k, 3); now.textContent = D.usd(k >= 1 ? to : from + (to - from) * e); if (k < 1 && dlg.open) requestAnimationFrame(step); else now.textContent = D.usd(to); };
      now.textContent = D.usd(from); requestAnimationFrame(step);
    }
    const mult = D.$('#rc-mult'); mult.hidden = T.total == null || !T.putIn; if (!mult.hidden) mult.textContent = (T.total / T.putIn).toFixed(2) + '×';
    const running = live.agents.filter((a) => a.running).length;
    const drawTiles = (tradeCount) => {
      const tiles = [['trades', tradeCount], ['passed', n ? n.passes : 'no data'], ['killed', n ? n.kills : 'no data'], ['agents', running]];
      D.$('#rc-tiles').replaceChildren(...tiles.map(([k, v]) => D.h('div', null, D.h('dt', { text: k }), D.h('dd', { text: String(v) }))));
    };
    drawTiles('…');
    const lines = D.$('#rc-lines');
    lines.replaceChildren(D.h('p', { class: 'dim', text: 'reading the trades' }));
    if (!dlg.open) dlg.showModal();
    try {
      const closed = (await D.json('/api/activity?limit=200')).closed.filter((t) => t.pnl_usd != null && (!n || t.closed_at >= n.started_at) && (D.state.view === 'both' || t.strategy === D.state.view));
      drawTiles(closed.length);   // the trades of what is shown, counted from the real closed trades
      const line = (label, t) => D.h('p', null, label + ' ', D.avatar(t.mint, t.symbol), ' ' + D.sym(t.symbol, t.mint) + ' ', D.h('span', { class: D.dir(t.pnl_usd), text: D.usd(t.pnl_usd, { sign: true }) }));
      const out = [];
      if (closed.length) {
        const best = closed.reduce((a, b) => (b.pnl_usd > a.pnl_usd ? b : a)), worst = closed.reduce((a, b) => (b.pnl_usd < a.pnl_usd ? b : a));
        const w = closed.filter((t) => t.pnl_usd > 0).length;
        out.push(line('best', best), line('worst', worst), D.h('p', { text: 'win rate ' + Math.round(100 * w / closed.length) + '% · ' + w + ' won, ' + (closed.length - w) + ' lost this session' }));
      } else out.push(D.h('p', { text: 'no closed trades this session yet' }));
      out.push(D.h('p', { class: 'dim', text: viewName + ' · ' + running + ' agents · paper money only' }));
      lines.replaceChildren(...out);
    } catch (e) { if (!(e instanceof D.AuthError)) lines.replaceChildren(D.h('p', { class: 'down', text: 'Could not read the trades: ' + e.message })); }
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
        D.h('div', { class: 'acct__btns' }, D.h('button', { class: 'btn', id: 'theme-btn-2', type: 'button', text: 'Light / dark', onclick: () => D.theme.toggle() }),
          D.h('button', { class: 'btn', id: 'lock-btn-2', type: 'button', text: 'Lock', onclick: () => D.emit('lock') })));
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
