/* Boot: login, polling with backoff, the live stream, tabs, banners. The last good numbers stay on screen when the desk goes away. */
(function () {
  'use strict';
  const D = window.D;
  D.symbols = {};
  let pollTimer = null, countdown = null, booted = false;

  /* ---------- banners and confetti (only for things that really happened) ---------- */
  D.banner = (title, sub, kind) => {
    const box = D.$('#banners');
    const el = D.h('div', { class: 'banner banner--' + (kind || 'flat'), role: 'status' }, title, sub ? D.h('small', { text: sub }) : null);
    box.append(el);
    while (box.children.length > 3) box.firstChild.remove();
    setTimeout(() => { el.classList.add('is-out'); setTimeout(() => el.remove(), 450); }, 6500);
  };
  D.toast = (msg, kind) => D.banner(msg, null, kind === 'ok' ? 'up' : kind === 'bad' ? 'down' : 'flat');

  D.confetti = () => {
    if (D.reduced()) return;
    const cv = D.$('#confetti'), ctx = cv.getContext('2d');
    cv.width = innerWidth; cv.height = innerHeight;
    const cols = [D.css('--green-hi'), D.css('--blue'), D.css('--analyst'), D.css('--brain'), D.css('--risk')];
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

  /* ---------- online / offline ---------- */
  function setOnline(ok) {
    const bar = D.$('#offline');
    if (ok) { D.state.online = true; D.state.fails = 0; D.state.lastUp = Date.now() / 1000; bar.hidden = true; clearInterval(countdown); return; }
    D.state.online = false; D.state.fails++;
    const wait = Math.min(30, Math.pow(2, D.state.fails));
    D.$('#last-up').textContent = D.state.lastUp ? D.clock(D.state.lastUp) : '--:--:--';
    bar.hidden = false;
    let left = wait;
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
    P.header(live); P.ledger(live); P.agents(live); P.log(live); P.positions(live); P.radar(live); P.kills(live); P.speed(live); P.health(live); P.recap(live); P.lane(live);
    live.decisions.forEach((r) => { if (r.mint && r.symbol) D.symbols[r.mint] = r.symbol; });
    D.flow.setSnapshot(live);
    P.auditFromLive(live);
    if (firstRender) { firstRender = false; loadChart(); P.account(); P.settings(); }
  }

  /* ---------- the live stream ---------- */
  let refreshSoon = null;
  const soon = (ms) => { clearTimeout(refreshSoon); refreshSoon = setTimeout(() => { poll(); loadChart(); }, ms); };
  D.on('event', (ev) => {
    const d = ev.data || {};
    D.flow.onEvent(ev);
    switch (ev.topic) {
      case 'decision': if (d.mint && d.symbol) D.symbols[d.mint] = d.symbol; D.panels.logEvent(d); break;
      case 'audit_layer': D.panels.auditEvent(d); break;
      case 'audit_pass': D.panels.auditDone(d); break;
      case 'trade_open': D.banner('Bought ' + (d.symbol || '') + ' · ' + D.usd(d.usd), (d.strategy === 'orion_fast' ? 'orion_fast' : 'Executor') + ' · paper', 'flat'); soon(1200); break;
      case 'trade_partial': D.banner('Took profit on ' + (d.symbol || ''), (d.reason || '') + ' · paper', 'up'); soon(1200); break;
      case 'trade_close': {
        const up = d.pnl_usd >= 0;
        D.banner('Sold ' + (d.symbol || '') + ' · ' + D.usd(d.pnl_usd, { sign: true }) + ' (' + D.pct(d.pnl_pct, { sign: true, d: 0 }) + ')', (d.reason || '') + ' · paper', up ? 'up' : 'down');
        if (d.pnl_usd > 5) D.confetti();   // only for a real profit above $5
        soon(1200); break;
      }
      case 'restart': D.banner('Restarted ' + d.bot, d.reason, 'down'); soon(800); break;
      case 'session_roll': soon(800); break;
      default: break;
    }
  });
  D.on('stream', (ok) => { if (ok) soon(300); else if (D.token()) poll(); });   // a dropped stream is noticed at once, not at the next poll

  /* ---------- login / lock ---------- */
  function showLogin(msg) {
    D.stopStream(); clearTimeout(pollTimer); clearInterval(countdown);
    D.$('#offline').hidden = true;
    const box = D.$('#login'), err = D.$('#login-error');
    box.hidden = false; document.body.style.overflow = 'hidden';
    err.hidden = !msg; err.textContent = msg || '';
    setTimeout(() => D.$('#token-input').focus(), 50);
  }
  D.on('auth-fail', () => { if (D.$('#login').hidden) { D.setToken(''); showLogin('The desk did not accept that token. Check the APP_TOKEN line in .env.'); } });
  D.$('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const t = D.$('#token-input').value.trim();
    if (!t) return;
    D.setToken(t);
    try { await D.json('/api/ping'); } catch (err) {
      D.setToken('');
      showLogin(err instanceof D.AuthError ? 'Token not accepted. Check the APP_TOKEN line in .env on the desk.' : 'Could not reach the desk. Is it running and on this network?');
      return;
    }
    D.$('#token-input').value = ''; D.$('#login').hidden = true; document.body.style.overflow = '';
    start();
  });
  D.$('#lock-btn').addEventListener('click', () => { D.setToken(''); booted = false; firstRender = true; D.state.live = null; showLogin(''); });

  /* ---------- tabs and controls ---------- */
  D.$$('#tabbar button').forEach((b) => b.addEventListener('click', () => {
    document.body.dataset.tab = b.dataset.go;
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
  D.$('#coin-close').addEventListener('click', () => D.$('#coin').close());
  D.$('#coin').addEventListener('click', (e) => { if (e.target === D.$('#coin')) D.$('#coin').close(); });

  /* ---------- masonry: each panel spans as many 8 px rows as it is tall (700 px and wider) ---------- */
  const ROW = 8, GAP = 14;
  const span = (el) => {
    if (innerWidth < 700) { el.style.gridRowEnd = ''; return; }
    el.style.gridRowEnd = 'span ' + Math.max(1, Math.ceil((el.getBoundingClientRect().height + GAP) / ROW));
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
  function start() {
    if (booted) return;
    booted = true;
    if (!inited) {   // one-time wiring: tickers and timers must not be registered twice after a lock/unlock
      inited = true;
      D.panels.logFilters();
      D.equity.init(); D.flow.init(); D.radar.init(); masonry();
      setInterval(() => { if (D.state.online && D.token()) loadChart(); }, 30000);
      setInterval(() => { if (D.token()) D.panels.frame(); }, 1000);
      setInterval(() => { if (D.state.online && D.token()) D.panels.account(); }, 60000);
    }
    poll();
    D.startStream();
    openReportFromHash();
  }

  D.theme.set(D.theme.get());
  document.body.classList.add('is-loading');
  if (D.token()) start(); else showLogin('');
})();
