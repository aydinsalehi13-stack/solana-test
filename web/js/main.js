/* Boot: splash (three real checks), connect, polling with backoff, the live stream, tabs, toasts.
   The last good numbers stay on screen when the desk goes away. Nothing here waits on a timer when the data is already in. */
(function () {
  'use strict';
  const D = window.D;
  D.symbols = {};
  let pollTimer = null, countdown = null, booted = false;
  const T0 = performance.now();
  const wait = (ms) => new Promise((res) => setTimeout(res, D.reduced() ? 0 : ms));

  /* ---------- toasts and confetti (only for things that really happened) ---------- */
  D.banner = (title, sub, kind, coin) => {
    const box = D.$('#banners');
    const el = D.h('div', { class: 'banner banner--' + (kind || 'flat'), role: 'status' },
      D.use('i-mark', 'banner__ico'), D.h('div', { class: 'banner__txt' }, title, sub ? D.h('small', { text: sub }) : null),
      coin && coin.mint ? D.avatar(coin.mint, coin.symbol, 'av--lg') : null);
    const out = () => { if (el.classList.contains('is-out')) return; el.classList.add('is-out'); setTimeout(() => el.remove(), 450); };
    el.addEventListener('click', out);
    box.append(el);
    while (box.children.length > 2) box.firstChild.remove();
    setTimeout(out, 6500);
  };
  D.toast = (msg, kind) => D.banner(msg, null, kind === 'ok' ? 'up' : kind === 'bad' ? 'down' : 'flat');

  D.confetti = () => {
    if (D.reduced()) return;
    const cv = D.$('#confetti'), ctx = cv.getContext('2d');
    cv.width = innerWidth; cv.height = innerHeight;
    const cols = [D.css('--green-hi'), D.css('--blue'), D.css('--analyst'), D.css('--scanner'), D.css('--risk')];
    const ps = Array.from({ length: 90 }, () => ({ x: innerWidth / 2 + (Math.random() - 0.5) * 120, y: innerHeight * 0.3, vx: (Math.random() - 0.5) * 12, vy: -Math.random() * 11 - 3, r: Math.random() * 6.3, c: cols[(Math.random() * cols.length) | 0], s: 4 + Math.random() * 5 }));
    const t0 = performance.now();
    const step = (t) => {
      const k = (t - t0) / 1600;
      ctx.clearRect(0, 0, cv.width, cv.height);
      if (k >= 1) return;
      for (const p of ps) { p.vy += 0.38; p.x += p.vx; p.y += p.vy; p.r += 0.2; ctx.save(); ctx.globalAlpha = 1 - k; ctx.translate(p.x, p.y); ctx.rotate(p.r); ctx.fillStyle = p.c; ctx.fillRect(-p.s / 2, -p.s / 4, p.s, p.s / 2); ctx.restore(); }
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  /* ---------- the splash: its three lines are the three real things the page waits for ---------- */
  const done = new Set();
  function bootStep(name, state, text) {
    const li = D.$('#boot-steps [data-step="' + name + '"]');
    li.classList.add('is-on'); li.classList.toggle('is-ok', state === 'ok'); li.classList.toggle('is-bad', state === 'bad');
    li.querySelector('b').textContent = text;
    if (state === 'ok' || state === 'bad') done.add(name);
    const k = done.size / 3;
    D.$('#boot-fill').style.transform = 'scaleX(' + k + ')';
    D.$('#boot-pct').textContent = Math.round(k * 100) + '%';
  }
  function bootReset() {
    done.clear();
    D.$$('#boot-steps li').forEach((li) => { li.className = ''; li.querySelector('b').textContent = ''; });
    D.$('#boot-fill').style.transform = 'scaleX(0)'; D.$('#boot-pct').textContent = '0%';
  }
  const splash = (on) => D.$('#splash').classList.toggle('is-out', !on);

  /* ---------- online / offline ---------- */
  function setOnline(ok) {
    const bar = D.$('#offline');
    if (ok) { D.state.online = true; D.state.fails = 0; D.state.lastUp = Date.now() / 1000; bar.hidden = true; clearInterval(countdown); return; }
    D.state.online = false; D.state.fails++;
    const secs = Math.min(30, Math.pow(2, D.state.fails));
    D.$('#last-up').textContent = D.state.lastUp ? D.clock(D.state.lastUp) : '--:--:--';
    bar.hidden = false;
    let left = secs;
    D.$('#retry-in').textContent = left;
    clearInterval(countdown);
    countdown = setInterval(() => { left--; D.$('#retry-in').textContent = Math.max(0, left); if (left <= 0) { clearInterval(countdown); poll(); } }, 1000);
  }

  /* ---------- polling ---------- */
  async function poll() {
    clearTimeout(pollTimer);
    try {
      const live = await D.json('/api/live');
      D.state.live = live;
      setOnline(true);
      render(live);
    } catch (e) {
      if (e instanceof D.AuthError) return;
      setOnline(false);
      return;
    }
    pollTimer = setTimeout(poll, document.hidden ? 60000 : 15000);
  }
  D.on('refresh', poll);

  async function loadChart() {
    try { D.equity.set(await D.json('/api/chart?range=' + D.state.range)); } catch (e) { if (!(e instanceof D.AuthError)) console.warn('chart not loaded', e.message); }
  }

  let firstRender = true;
  function render(live) {
    document.body.classList.remove('is-loading');
    D.$$('.skeleton').forEach((e) => e.classList.remove('skeleton'));
    const P = D.panels;
    P.header(live); P.ticker(live); P.ledger(live); P.agents(live); P.log(live); P.positions(live); P.radar(live); P.kills(live); P.speed(live); P.health(live); P.recap(live); P.lane(live);
    live.decisions.forEach((r) => { if (r.mint && r.symbol) D.symbols[r.mint] = r.symbol; });
    D.flow.setSnapshot(live);
    P.auditFromLive(live);
    if (firstRender) { firstRender = false; loadChart(); P.account(); P.settings(); }
  }

  /* ---------- the live stream ---------- */
  let refreshSoon = null;
  const soon = (ms) => { clearTimeout(refreshSoon); refreshSoon = setTimeout(() => { poll(); loadChart(); }, ms); };
  const who = (d) => (d.strategy === 'orion_fast' ? 'orion_fast' : 'Executor');
  D.on('event', (ev) => {
    const d = ev.data || {};
    D.flow.onEvent(ev);
    switch (ev.topic) {
      case 'decision': if (d.mint && d.symbol) D.symbols[d.mint] = d.symbol; D.panels.logEvent(d); break;
      case 'audit_layer': D.panels.auditEvent(d); break;
      case 'audit_pass': D.panels.auditDone(d); break;
      case 'trade_open': D.banner('Bought ' + D.sym(d.symbol, d.mint) + ' · ' + D.usd(d.usd), who(d) + ' · paper', 'flat', d); soon(1200); break;
      case 'trade_partial': D.banner('Took profit on ' + D.sym(d.symbol, d.mint), (d.reason || '') + ' · paper', 'up', d); soon(1200); break;
      case 'trade_close': {
        const up = d.pnl_usd >= 0;
        D.banner(['Sold ' + D.sym(d.symbol, d.mint) + ' · ', D.h('b', { text: D.usd(d.pnl_usd, { sign: true }) + ' (' + D.pct(d.pnl_pct, { sign: true, d: 0 }) + ')' })], (d.reason || '') + ' · paper · ' + (d.strategy || 'prompt'), up ? 'up' : 'down', d);
        if (d.pnl_usd > 5) D.confetti();   // only for a real profit above $5
        soon(1200); break;
      }
      case 'restart': D.banner('Restarted ' + d.bot, d.reason, 'down'); soon(800); break;
      case 'session_roll': soon(800); break;
      default: break;
    }
  });
  D.on('stream', (ok) => {
    D.panels.stream(ok);
    if (ok) { bootStep('stream', 'ok', 'open'); soon(300); } else if (D.token()) poll();   // a dropped stream is noticed at once, not at the next poll
  });

  /* ---------- connect / lock ---------- */
  function showLogin(msg) {
    D.stopStream(); clearTimeout(pollTimer); clearInterval(countdown);
    D.$('#offline').hidden = true;
    booted = false; firstRender = true;
    document.body.classList.remove('is-ready', 'is-settled');
    document.body.classList.add('is-login');
    bootReset(); splash(true);   // the dashboard is never left visible behind the connect sheet
    const box = D.$('#login'), err = D.$('#login-error'), btn = D.$('#connect-btn');
    box.classList.remove('is-out'); box.hidden = false; document.body.style.overflow = 'hidden';
    btn.classList.remove('is-ok', 'is-busy'); btn.firstElementChild.textContent = 'Connect';
    err.hidden = !msg; err.textContent = msg || '';
    if (!window.matchMedia('(pointer: coarse)').matches) setTimeout(() => D.$('#token-input').focus(), 50);
  }
  D.on('auth-fail', () => { if (D.$('#login').hidden) { D.setToken(''); showLogin('The desk did not accept that token. Check the APP_TOKEN line in .env.'); } });
  D.$('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const t = D.$('#token-input').value.trim(), btn = D.$('#connect-btn');
    if (!t || btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    D.setToken(t);
    try { await D.json('/api/ping'); } catch (err) {
      D.setToken('');
      showLogin(err instanceof D.AuthError ? 'Token not accepted. Check the APP_TOKEN line in .env on the desk.' : 'Could not reach the desk. Is it running and on this network?');
      return;
    }
    D.$('#token-input').value = '';
    btn.classList.remove('is-busy'); btn.classList.add('is-ok'); btn.firstElementChild.textContent = 'Connected';
    await wait(520);
    D.$('#login').classList.add('is-out');
    await wait(360);
    D.$('#login').hidden = true; document.body.style.overflow = ''; document.body.classList.remove('is-login');
    start(true);
  });
  const lock = () => { D.setToken(''); D.state.live = null; showLogin(''); };
  D.on('lock', lock);
  D.$('#lock-btn').addEventListener('click', lock);

  /* ---------- setup: after a fresh connect, each agent turns "ready" only if its real heartbeat is fresh ---------- */
  async function runSetup(live) {
    const box = D.$('#setup'), cards = D.$('#setup-cards');
    let skipped = false;
    const skip = () => { skipped = true; };
    box.addEventListener('click', skip, { once: true });
    const n = live.night;
    D.$('#setup-line').textContent = live.strategies.length + ' strategies → ' + live.agents.length + ' agents' + (n ? ' · session ' + D.hm(n.started_at) + (n.ends_at ? ' → ' + D.hm(n.ends_at) : '') : '');
    const by = Object.fromEntries(live.agents.map((a) => [a.name, a]));
    const chip = (key, label, ready) => { const c = D.h('span', { class: 'rchip', style: '--c:' + (D.AGENTS[key] ? D.agentColor(key) : D.css('--green')) }, D.use('i-bot'), label, D.h('small', { text: '…' })); c.dataset.ready = ready ? '1' : ''; return c; };
    const card = (dot, title, side, sub, chips) => D.h('div', { class: 'sc' }, D.h('div', { class: 'sc__head' }, D.h('i', { class: 'sdot', style: 'background:' + dot }), title, D.h('small', { text: side })), D.h('p', { class: 'sc__sub', text: sub }), D.h('div', { class: 'sc__chips' }, chips));
    const sOf = (name) => live.strategies.find((s) => s.name === name) || {};
    const feed = live.feed ? live.feed.badge : 'no feed';
    cards.replaceChildren(
      card(D.css('--prompt'), 'prompt', D.isNum(sOf('prompt').balance) ? D.usd(sOf('prompt').balance) : 'no balance yet', 'scan → audit → size → buy the dip → guard', ['scanner', 'analyst', 'brain', 'executor', 'risk'].map((k) => chip(k, D.AGENTS[k].label.toUpperCase(), by[k] && by[k].running))),
      card(D.css('--orion'), 'orion_fast', D.isNum(sOf('orion_fast').balance) ? D.usd(sOf('orion_fast').balance) : 'no balance yet', 'fast momentum · paper only', [chip('orion', 'ORION', by.orion && by.orion.running)]),
      card(D.css('--green-hi'), 'feed', feed, 'new coins and prices', [chip('feed', 'FEED', !!live.feed && feed !== 'feed down')]));
    box.classList.remove('is-out'); box.hidden = false;
    splash(false);
    const all = D.$$('.sc', cards), chips = D.$$('.rchip', cards);
    for (const c of all) { if (!skipped) await wait(110); c.classList.add('is-on'); }
    if (!skipped) await wait(260);
    for (const c of chips) {
      if (!skipped) await wait(105);
      const ok = c.dataset.ready === '1';
      c.classList.add(ok ? 'is-ready' : 'is-off'); c.querySelector('small').textContent = ok ? '✓ ready' : 'offline';
    }
    if (!skipped) await wait(650);
    document.body.classList.add('is-ready');
    box.classList.add('is-out');
    await wait(520);
    box.hidden = true;
  }

  /* ---------- tabs and controls ---------- */
  D.$$('#tabbar button').forEach((b) => b.addEventListener('click', () => {
    document.body.dataset.tab = b.dataset.go;
    document.body.classList.add('is-tabbing'); setTimeout(() => document.body.classList.remove('is-tabbing'), 520);
    D.$$('#tabbar button').forEach((x) => { if (x !== b) x.removeAttribute('aria-current'); else x.setAttribute('aria-current', 'page'); });
    scrollTo({ top: 0 });
    window.dispatchEvent(new Event('resize'));   // canvases in the newly shown section measure themselves
  }));
  D.$$('#range-tabs button').forEach((b) => b.addEventListener('click', () => {
    D.state.range = b.dataset.range;
    D.$$('#range-tabs button').forEach((x) => x.setAttribute('aria-selected', String(x === b)));
    loadChart();
  }));
  D.$$('#pos-tabs button').forEach((b) => b.addEventListener('click', () => D.panels.posTab(b.dataset.pos)));
  D.$('#theme-btn').addEventListener('click', () => D.theme.toggle());
  D.on('open-coin', (mint, sym) => D.panels.openCoin(mint, sym));
  const backdropCloses = (id) => D.$(id).addEventListener('click', (e) => { if (e.target === D.$(id)) D.$(id).close(); });
  D.$('#coin-close').addEventListener('click', () => D.$('#coin').close());
  backdropCloses('#coin'); backdropCloses('#recap-sheet');
  D.$('#hero-in-chip').addEventListener('click', () => D.panels.openRecap());
  D.$('#recap-open').addEventListener('click', () => D.panels.openRecap());
  D.$('#recap-close').addEventListener('click', () => D.$('#recap-sheet').close());
  D.$('#rc-report').addEventListener('click', async () => {
    const note = D.$('#rc-note');
    note.className = 'sheet__note'; note.textContent = 'Writing the report from the desk database';
    try {
      const r = await D.json('/api/report/generate', { method: 'POST' });
      D.$('#recap-sheet').close();
      if (location.hash === '#report=' + r.date) openReportFromHash(); else location.hash = '#report=' + r.date;
    } catch (e) { if (!(e instanceof D.AuthError)) { note.className = 'sheet__note is-bad'; note.textContent = 'Could not write the report: ' + e.message; } }
  });

  /* ---------- masonry: each panel spans as many 8 px rows as it is tall (700 px and wider) ---------- */
  const ROW = 8, GAP = 14;
  const span = (el) => {
    if (innerWidth < 700) { el.style.gridRowEnd = ''; return; }
    el.style.gridRowEnd = 'span ' + Math.max(1, Math.ceil((el.offsetHeight + GAP) / ROW));
  };
  function masonry() {
    const ro = new ResizeObserver((es) => es.forEach((e) => span(e.target)));
    D.$$('.grid > *').forEach((el) => ro.observe(el));
    window.addEventListener('resize', () => D.$$('.grid > *').forEach(span));
  }

  /* ---------- a night report link (#report=2026-11-18): fetched with the token, shown without scripts ---------- */
  async function openReportFromHash() {
    const m = /^#report=(\d{4}-\d{2}-\d{2})$/.exec(location.hash);
    if (!m || !D.token()) return;
    try {
      const r = await D.fetch('/api/report/' + m[1]);
      if (!r.ok) { D.toast('No report for ' + m[1], 'bad'); return; }
      D.$('#report-title').textContent = 'Night report ' + m[1];
      D.$('#report-frame').srcdoc = await r.text();
      if (!D.$('#report').open) D.$('#report').showModal();
    } catch (e) { if (!(e instanceof D.AuthError)) D.toast('Could not load the report', 'bad'); }
  }
  window.addEventListener('hashchange', openReportFromHash);
  D.$('#report-close').addEventListener('click', () => { D.$('#report').close(); history.replaceState(null, '', location.pathname); });

  /* ---------- start ---------- */
  let inited = false;
  async function start(fresh) {
    if (booted) return;
    booted = true;
    if (!inited) {   // one-time wiring: tickers and timers must not be registered twice after a lock/unlock
      inited = true;
      D.panels.logFilters();
      D.equity.init(); D.flow.init(); D.radar.init(); masonry();
      setInterval(() => { if (D.state.online && D.token()) loadChart(); }, 30000);
      setInterval(() => { if (D.token()) D.panels.frame(); }, 1000);
      setInterval(() => { if (D.state.online && D.token()) D.panels.account(); }, 60000);
      setInterval(() => D.panels.clock(), 5000);
    }
    bootReset(); splash(true);
    bootStep('ping', 'wait', '…');
    if (fresh) bootStep('ping', 'ok', 'ok');   // the connect sheet already asked /api/ping with this token
    else {
      try { await D.json('/api/ping'); bootStep('ping', 'ok', 'ok'); } catch (e) { if (e instanceof D.AuthError) return; bootStep('ping', 'bad', 'no answer'); }
    }
    bootStep('live', 'wait', '…');
    await poll();
    if (!booted) return;   // the token was refused meanwhile: the connect sheet is up
    const live = D.state.live;
    bootStep('live', live ? 'ok' : 'bad', live ? live.agents.filter((a) => a.running).length + ' agents' : 'offline');
    bootStep('stream', 'wait', '…');
    D.startStream();
    openReportFromHash();
    const floor = 620 - (performance.now() - T0);   // never a flash: the splash is on screen for at least ~0.6 s after the page opened
    if (floor > 0) await wait(floor);
    if (!booted) return;
    if (fresh && live && !D.reduced()) await runSetup(live);
    else { splash(false); document.body.classList.add('is-ready'); }
    setTimeout(() => document.body.classList.add('is-settled'), 1400);
  }

  D.theme.set(D.theme.get());
  document.body.classList.add('is-loading');
  if (D.token()) start(false); else showLogin('');
})();
