/* core: formatting, DOM helpers, the API client, the live event stream, theme, and one shared 30 fps ticker. */
(function () {
  'use strict';
  const D = (window.D = { state: { live: null, range: 'night', online: true, fails: 0, lastUp: null }, handlers: {} });

  D.on = (name, fn) => ((D.handlers[name] = D.handlers[name] || []).push(fn), fn);
  D.emit = (name, ...args) => (D.handlers[name] || []).forEach((fn) => {
    try { fn(...args); } catch (e) { console.error('handler failed for ' + name, e); }
  });

  /* ---------- formatting (a missing value is 'no data', never a made-up number) ---------- */
  const MINUS = '−';
  D.MINUS = MINUS;
  const isNum = (n) => typeof n === 'number' && isFinite(n);
  D.isNum = isNum;
  D.usd = (n, o = {}) => {
    if (!isNum(n)) return 'no data';
    const d = o.d === undefined ? 2 : o.d;
    const r = Number(n.toFixed(d));
    const body = Math.abs(r).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
    return (r < 0 ? MINUS : o.sign && r > 0 ? '+' : '') + '$' + body;
  };
  D.pct = (n, o = {}) => {
    if (!isNum(n)) return 'no data';
    const d = o.d === undefined ? 1 : o.d;
    const r = Number(n.toFixed(d));
    return (r < 0 ? MINUS : o.sign && r > 0 ? '+' : '') + Math.abs(r).toFixed(d) + '%';
  };
  D.dir = (n) => (!isNum(n) || Math.abs(n) < 0.005 ? 'flat' : n > 0 ? 'up' : 'down');
  D.arrow = (n) => (D.dir(n) === 'up' ? '▲' : D.dir(n) === 'down' ? '▼' : '◆');
  D.price = (p) => {
    if (!isNum(p)) return 'no data';
    if (p >= 1) return p.toFixed(4);
    const s = p.toPrecision(3);
    return /e/.test(s) ? Number(s).toExponential(2) : s;
  };
  D.pad = (n) => String(n).padStart(2, '0');
  D.clock = (ts) => { const d = new Date(ts * 1000); return D.pad(d.getHours()) + ':' + D.pad(d.getMinutes()) + ':' + D.pad(d.getSeconds()); };
  D.durHM = (s) => { s = Math.max(0, s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h + 'h ' + m + 'm'; };
  D.hm = (ts) => { const d = new Date(ts * 1000); return D.pad(d.getHours()) + ':' + D.pad(d.getMinutes()); };
  D.ago = (ts) => {
    const s = Math.max(0, Date.now() / 1000 - ts);
    return s < 90 ? Math.round(s) + 's' : s < 5400 ? Math.round(s / 60) + 'm' : s < 172800 ? Math.round(s / 3600) + 'h' : Math.round(s / 86400) + 'd';
  };
  D.dur = (s) => (s < 90 ? Math.round(s) + 's' : s < 5400 ? Math.round(s / 60) + 'm' : (s / 3600).toFixed(1) + 'h');
  D.ms = (v) => (isNum(v) ? (v < 10 ? v.toFixed(1) : Math.round(v)) + ' ms' : 'no data');

  /* ---------- DOM ---------- */
  D.h = (tag, props, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === false || v == null) continue;
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k === 'style') e.style.cssText = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(c));
    return e;
  };
  D.svg = (tag, attrs, ...kids) => {
    const e = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
    for (const c of kids.flat()) if (c != null) e.append(c.nodeType ? c : document.createTextNode(c));
    return e;
  };
  D.$ = (sel, root) => (root || document).querySelector(sel);
  D.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  D.css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  D.reduced = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  D.hash = (str) => { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; };

  /* a coin's avatar: we store no coin images, so each coin gets a stable color of its own and its first letter */
  D.avatar = (mint, symbol, cls) => D.h('span', { class: 'av' + (cls ? ' ' + cls : ''), style: '--h:' + Math.round(D.hash(String(mint || symbol || '?')) * 360), 'aria-hidden': 'true', text: String(symbol || mint || '?').replace(/^\$/, '').slice(0, 1) });
  D.sym = (symbol, mint) => '$' + (symbol || (mint || '').slice(0, 6) || '?');
  D.use = (id, cls) => { const s = D.svg('svg', { class: cls || null, viewBox: id === 'i-bot' ? '0 0 32 32' : '0 0 100 100', 'aria-hidden': 'true' }); s.append(D.svg('use', { href: '#' + id })); return s; };

  D.AGENTS = {
    scanner: { label: 'Scanner', c: '--scanner' }, analyst: { label: 'Analyst', c: '--analyst' }, brain: { label: 'Brain', c: '--brain' },
    executor: { label: 'Executor', c: '--executor' }, risk: { label: 'Risk', c: '--risk' }, orion: { label: 'orion', c: '--orion-agent' },
  };
  D.agentColor = (name) => D.css((D.AGENTS[name] || { c: '--label' }).c) || '#9ca3af';
  D.LAYERS = ['Mint authority', 'Freeze authority', 'Dev wallet', 'Top 10', 'Liquidity', 'Pool age', 'Bundles', 'Delegations'];

  /* ---------- token + API (the token lives in this browser only and travels in a header, never in a URL) ---------- */
  class AuthError extends Error {}
  D.AuthError = AuthError;
  const KEY = 'desk_token';
  D.token = () => { try { return localStorage.getItem(KEY) || ''; } catch (e) { return ''; } };
  D.setToken = (t) => { try { if (t) localStorage.setItem(KEY, t); else localStorage.removeItem(KEY); } catch (e) { /* private mode */ } };
  D.fetch = async (path, o = {}) => {
    const headers = { Authorization: 'Bearer ' + D.token() };
    if (o.body !== undefined) headers['content-type'] = 'application/json';
    const r = await fetch(path, { method: o.method || 'GET', headers, body: o.body === undefined ? undefined : JSON.stringify(o.body), cache: 'no-store', signal: o.signal });
    if (r.status === 401) { D.emit('auth-fail'); throw new AuthError('token not accepted'); }
    return r;
  };
  D.json = async (path, o) => {
    const r = await D.fetch(path, o);
    let j = null;
    try { j = await r.json(); } catch (e) { /* not json */ }
    if (!r.ok) { const err = new Error((j && j.error) || 'HTTP ' + r.status); err.status = r.status; err.data = j; throw err; }
    return j;
  };

  /* ---------- the live event stream: fetch + reader (EventSource cannot send the token header) ---------- */
  let streamAbort = null;
  let streamOn = false;
  D.startStream = async () => {
    if (streamOn) return;
    streamOn = true;
    let delay = 1;
    while (streamOn) {
      streamAbort = new AbortController();
      try {
        const r = await D.fetch('/api/stream', { signal: streamAbort.signal });
        if (!r.ok || !r.body) throw new Error('stream HTTP ' + r.status);
        const reader = r.body.getReader();
        const dec = new TextDecoder();
        let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          let i;
          while ((i = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, i);
            buf = buf.slice(i + 2);
            if (chunk.startsWith('data: ')) {
              let ev = null;
              try { ev = JSON.parse(chunk.slice(6)); } catch (e) { /* ignore a torn frame */ }
              if (ev) { if (ev.topic === 'hello') { delay = 1; D.emit('stream', true); } else D.emit('event', ev); }
            }
          }
        }
      } catch (e) {
        if (e instanceof AuthError || !streamOn) return;
      }
      D.emit('stream', false);
      if (!streamOn) return;
      await new Promise((res) => setTimeout(res, delay * 1000));
      delay = Math.min(30, delay * 2);
    }
  };
  D.stopStream = () => { streamOn = false; if (streamAbort) streamAbort.abort(); };

  /* ---------- theme ---------- */
  D.theme = {
    get: () => { try { return localStorage.getItem('desk_theme') === 'dark' ? 'dark' : 'light'; } catch (e) { return 'light'; } },
    set: (t) => { document.documentElement.dataset.theme = t; try { localStorage.setItem('desk_theme', t); } catch (e) { /* ignore */ } const m = document.querySelector('meta[name=theme-color]'); if (m) m.content = t === 'light' ? '#f3f4f6' : '#000000'; D.emit('theme'); },
    toggle: () => D.theme.set(D.theme.get() === 'light' ? 'dark' : 'light'),
  };

  /* ---------- one shared ticker: 30 fps cap, paused while the tab is hidden ---------- */
  const tickers = [];
  D.ticker = (fn) => tickers.push(fn);
  let last = 0;
  function frame(t) {
    requestAnimationFrame(frame);
    if (document.hidden || t - last < 33) return;
    const dt = t - last;
    last = t;
    for (const fn of tickers) { try { fn(t, dt); } catch (e) { console.error('ticker failed', e); } }
  }
  requestAnimationFrame(frame);

  /* ---------- sizing helper for crisp canvases ---------- */
  D.fit = (canvas) => {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  };
})();
