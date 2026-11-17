/* The pipeline river and the radar.
   Nothing here is decorative: a dot enters the Scanner when a coin really reached the waiting room, moves to the Analyst when the
   Scanner really passed it, an audit layer really landed, and so on. A kill falls away in red. When nothing happens, nothing moves,
   except the pulse each agent makes when its real heartbeat arrives. */
(function () {
  'use strict';
  const D = window.D;

  /* ---------------------------------- pipeline ---------------------------------- */
  const F = (D.flow = {
    canvas: null, nodes: {}, particles: [], pulses: [], pips: Array(8).fill(null), pipMint: null, lastAge: {}, rest: { prompt: 0, orion: 0 }, dirtyUntil: 0, layout: null,

    init() {
      this.canvas = D.$('#flow');
      new ResizeObserver(() => { this.layout = null; this.dirtyUntil = performance.now() + 400; }).observe(this.canvas);
      D.on('theme', () => { this.dirtyUntil = performance.now() + 400; });
      D.ticker((t, dt) => this.tick(t, dt));
    },

    place(w, h) {
      const mobile = w < 480, pad = mobile ? 34 : 56;
      const xs = (n, from, to) => Array.from({ length: n }, (_, i) => from + ((to - from) * i) / (n - 1));
      const x5 = xs(5, pad + 14, w - pad - 14);
      const y1 = h * 0.3, y2 = h * 0.76;
      const names = ['scanner', 'analyst', 'brain', 'executor', 'risk'];
      const n = {};
      names.forEach((k, i) => (n[k] = { key: k, x: x5[i], y: y1, lane: 'prompt', r: mobile ? 11 : 15 }));
      n.feed = { key: 'feed', x: pad - 6, y: y2, lane: 'orion', r: 7 };
      n.entry = { key: 'entry', x: 4, y: y1 }; n.entry2 = { key: 'entry2', x: 4, y: y2 }; n.out = { key: 'out', x: w - 4, y: y1 };
      n.orion = { key: 'orion', x: x5[2], y: y2, lane: 'orion', r: mobile ? 11 : 15 };
      this.layout = { w, h, mobile };
      // reposition, but keep what the desk told us (captions, running/paused) when the canvas is resized
      this.nodes = Object.assign(this.nodes, Object.fromEntries(Object.entries(n).map(([k, v]) => [k, Object.assign(this.nodes[k] || { sub: '' }, v)])));
    },

    /* what the desk reports right now (counts and heartbeats) */
    setSnapshot(live) {
      if (!live) return;
      const eng = live.engines || {}, byName = Object.fromEntries((live.agents || []).map((a) => [a.name, a]));
      const sum = (a) => (a ? a.rate.reduce((x, y) => x + y, 0) : 0);
      const set = (k, sub) => { if (this.nodes[k]) this.nodes[k].sub = sub; };
      set('scanner', eng.prompt ? 'room ' + eng.prompt.room : '');
      set('analyst', sum(byName.analyst) + '/10m');
      set('brain', sum(byName.brain) + '/10m');
      set('executor', eng.prompt ? 'waiting ' + eng.prompt.waiting : '');
      set('risk', eng.prompt ? 'open ' + eng.prompt.open : '');
      set('orion', eng.orion_fast ? 'open ' + eng.orion_fast.open : '');
      set('feed', '');
      this.rest.prompt = eng.prompt ? eng.prompt.open : 0;
      this.rest.orion = eng.orion_fast ? eng.orion_fast.open : 0;
      for (const a of live.agents || []) {   // a heartbeat that really arrived: its node pulses once
        const prev = this.lastAge[a.name];
        if (prev != null && a.heartbeat_age_s != null && a.heartbeat_age_s + 0.4 < prev) this.pulse(a.name);
        this.lastAge[a.name] = a.heartbeat_age_s;
        const nd = this.nodes[a.name];
        if (nd) { nd.running = a.running; nd.paused = a.paused; }
      }
      this.dirtyUntil = performance.now() + 300;
    },

    pulse(key) { this.pulses.push({ key, t0: performance.now() }); this.dirtyUntil = performance.now() + 1300; },

    send(from, to, color, label, dur) {
      const a = this.nodes[from], b = this.nodes[to];
      if (!a || !b || this.particles.length > 70) return;
      this.particles.push({ kind: 'move', a, b, t0: performance.now(), dur: D.reduced() ? 1 : dur || 900, color, label });
      this.dirtyUntil = performance.now() + (dur || 900) + 200;
    },
    drop(from, color, label) {
      const a = this.nodes[from];
      if (!a || this.particles.length > 70) return;
      this.particles.push({ kind: 'drop', a, t0: performance.now(), dur: 1300, color, label, dx: (Math.random() - 0.5) * 26 });
      this.dirtyUntil = performance.now() + 1500;
    },

    /* a real event from the desk */
    onEvent(ev) {
      const d = ev.data || {}, red = D.css('--red-hi'), green = D.css('--green-hi');
      if (!this.nodes.scanner) return;
      switch (ev.topic) {
        case 'room_add': this.send('entry', 'scanner', D.agentColor('scanner'), d.symbol, 800); break;
        case 'new_token': this.send('entry2', 'orion', D.agentColor('orion'), null, 1100); break;
        case 'scan_pass': this.send('scanner', 'analyst', D.agentColor('scanner'), d.symbol); break;
        case 'audit_layer':
          if (d.layer === 1 || d.mint !== this.pipMint) { this.pips = Array(8).fill(null); this.pipMint = d.mint; }
          this.pips[d.layer - 1] = d.pass === 1 ? true : d.pass === 0 ? false : 'off';
          this.pulse('analyst'); break;
        case 'audit_pass': this.send('analyst', 'brain', D.agentColor('analyst'), d.symbol); break;
        case 'order_request': this.send('brain', 'executor', D.agentColor('brain'), d.symbol); break;
        case 'trade_open': d.strategy === 'orion_fast' ? this.pulse('orion') : this.send('executor', 'risk', D.agentColor('executor'), d.symbol); break;
        case 'trade_close': case 'trade_partial': {
          const win = (d.pnl_usd != null ? d.pnl_usd : d.proceeds) >= 0, key = d.strategy === 'orion_fast' ? 'orion' : 'risk', a = this.nodes[key];
          this.nodes.out.y = a.y;
          this.send(key, 'out', win ? green : red, d.symbol, 1000); break;
        }
        case 'decision': {
          const bad = ['kill', 'reject', 'skip', 'drop'].includes(d.decision), bot = d.bot;
          if (bad && this.nodes[bot]) this.drop(bot, red, bucketShort(d.reason));
          else if (d.decision === 'wait' || d.decision === 'size') this.pulse(bot);
          break;
        }
        default: break;
      }
    },

    active(now) { return now < this.dirtyUntil || this.particles.length > 0 || this.pulses.length > 0; },

    tick(now) {
      if (!this.canvas || !this.canvas.offsetParent) return;
      if (!this.active(now) && this.layout) return;   // idle: no redraw at all
      const { ctx, w, h } = D.fit(this.canvas);
      if (!this.layout || this.layout.w !== w || this.layout.h !== h) this.place(w, h);
      this.draw(ctx, w, h, now);
    },

    draw(ctx, w, h, now) {
      ctx.clearRect(0, 0, w, h);
      const N = this.nodes, hair = D.css('--hair-strong'), label = D.css('--label'), muted = D.css('--muted'), glass = D.css('--glass');
      ctx.font = '600 11px ' + D.css('--font-ui'); ctx.textBaseline = 'middle';
      // lanes: a faint line through each lane's nodes, with the lane's name at the far left
      const lane = (keys, y, name, col) => {
        ctx.strokeStyle = hair; ctx.lineWidth = 1; ctx.setLineDash([2, 6]); ctx.beginPath(); ctx.moveTo(N[keys[0]].x, y); ctx.lineTo(w - 8, y); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = col; ctx.fillRect(0, y - 20 - 26, 3, 14);
        ctx.fillStyle = muted; ctx.textAlign = 'left'; ctx.fillText(name, 10, y - 39);
      };
      lane(['scanner'], N.scanner.y, 'prompt', D.css('--prompt'));
      lane(['feed'], N.orion.y, 'orion_fast', D.css('--orion'));
      // nodes
      for (const k of ['scanner', 'analyst', 'brain', 'executor', 'risk', 'feed', 'orion']) {
        const n = N[k]; if (!n) continue;
        const col = k === 'feed' ? muted : D.agentColor(k), off = n.running === false || n.paused;
        ctx.globalAlpha = off ? 0.45 : 1;
        ctx.fillStyle = glass; ctx.strokeStyle = col; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, 6.3); ctx.fill(); ctx.stroke();
        ctx.fillStyle = col; ctx.beginPath(); ctx.arc(n.x, n.y, Math.max(2.5, n.r * 0.3), 0, 6.3); ctx.fill();
        ctx.globalAlpha = 1;
        ctx.fillStyle = label; ctx.textAlign = 'center'; ctx.fillText(k === 'feed' ? 'Feed' : D.AGENTS[k].label, n.x, n.y + n.r + 13);
        ctx.fillStyle = muted; ctx.font = '11px ' + D.css('--font-ui'); ctx.fillText(n.sub || '', n.x, n.y + n.r + 27); ctx.font = '600 11px ' + D.css('--font-ui');
      }
      // heartbeat pulses (real heartbeats only)
      this.pulses = this.pulses.filter((p) => now - p.t0 < 1200);
      for (const p of this.pulses) {
        const n = N[p.key]; if (!n) continue;
        const k = (now - p.t0) / 1200;
        ctx.strokeStyle = D.agentColor(p.key); ctx.globalAlpha = 0.7 * (1 - k); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 3 + k * 16, 0, 6.3); ctx.stroke(); ctx.globalAlpha = 1;
      }
      // the Analyst's 8 audit pips: filled as each layer lands
      const an = N.analyst, step = 8;
      this.pips.forEach((v, i) => {
        const x = an.x + (i - 3.5) * step, y = an.y - an.r - 11;
        ctx.beginPath(); ctx.arc(x, y, 2.6, 0, 6.3);
        if (v === null) { ctx.strokeStyle = hair; ctx.lineWidth = 1; ctx.stroke(); } else { ctx.fillStyle = v === true ? D.css('--green-hi') : v === false ? D.css('--red-hi') : muted; ctx.fill(); }
      });
      // positions being held: small dots under Risk / orion
      const holds = (n, count, col) => { for (let i = 0; i < Math.min(count, 8); i++) { ctx.fillStyle = col; ctx.beginPath(); ctx.arc(n.x - ((Math.min(count, 8) - 1) * 4.5) + i * 9, n.y - n.r - 11, 3, 0, 6.3); ctx.fill(); } };
      holds(N.risk, this.rest.prompt, D.css('--green-hi')); holds(N.orion, this.rest.orion, D.css('--green-hi'));
      // particles
      this.particles = this.particles.filter((p) => now - p.t0 < p.dur + 250);
      ctx.textAlign = 'left';
      let labels = 0;
      for (const p of this.particles) {
        const k = Math.min(1, (now - p.t0) / p.dur);
        let x, y, a = 1;
        if (p.kind === 'move') { const e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2; x = p.a.x + (p.b.x - p.a.x) * e; y = p.a.y + (p.b.y - p.a.y) * e; a = now - p.t0 > p.dur ? 1 - (now - p.t0 - p.dur) / 250 : 1; }
        else { x = p.a.x + p.dx * k; y = p.a.y + p.a.r + 36 + k * 34; a = 1 - k; }
        ctx.globalAlpha = Math.max(0, a); ctx.fillStyle = p.color; ctx.shadowColor = p.color; ctx.shadowBlur = D.reduced() ? 0 : 10;
        ctx.beginPath(); ctx.arc(x, y, p.kind === 'drop' ? 3 : 4, 0, 6.3); ctx.fill(); ctx.shadowBlur = 0;
        if (p.label && labels < 5) { labels++; ctx.fillStyle = p.kind === 'drop' ? p.color : label; ctx.font = '600 10.5px ' + D.css('--font-ui'); ctx.fillText(p.label, x + 7, y - (p.kind === 'drop' ? 0 : 9)); }
        ctx.globalAlpha = 1;
      }
    },
  });

  function bucketShort(reason) {
    const r = reason || '';
    const m = [[/^liq/, 'liquidity'], [/^top 10/, 'top 10'], [/^1h \+/, 'pumped'], [/^copycat/, 'copycat'], [/^FOMO/, 'FOMO'], [/^holders/, 'holders'], [/^layer (\d)/, null], [/^score/, 'score'], [/^no dip/, 'no dip'], [/^crash/, 'crash'], [/^entries paused/, 'paused'], [/^under/, 'too small']];
    for (const [re, name] of m) { const x = re.exec(r); if (x) return name || 'layer ' + x[1]; }
    return r.split(' ').slice(0, 2).join(' ');
  }
  D.bucketShort = bucketShort;

  /* ---------------------------------- radar ---------------------------------- */
  const R = (D.radar = {
    canvas: null, blips: [], sweep: 0, visible: true, hover: null, dirty: true,

    init() {
      this.canvas = D.$('#radar');
      const tip = D.$('#radar-tip');
      new IntersectionObserver((es) => { this.visible = es.some((e) => e.isIntersecting); }).observe(this.canvas);
      const show = (e) => {
        const r = this.canvas.getBoundingClientRect(), x = (e.clientX ?? e.touches[0].clientX) - r.left, y = (e.clientY ?? e.touches[0].clientY) - r.top;
        let best = null, bd = 18;
        for (const b of this.blips) { const d = Math.hypot(b.px - x, b.py - y); if (d < bd) { bd = d; best = b; } }
        if (!best) { tip.hidden = true; return; }
        tip.replaceChildren(D.h('b', { text: best.symbol || best.mint.slice(0, 6) }), D.h('small', { text: (best.decision === 'pass' ? 'passed' : 'killed') + ' · ' + D.ago(best.ts) + ' ago' }), D.h('small', { text: best.reason || '' }));
        tip.hidden = false; tip.style.left = Math.min(r.width - 170, Math.max(0, best.px + 10)) + 'px'; tip.style.top = Math.max(0, best.py - 20) + 'px';
      };
      this.canvas.addEventListener('pointermove', show);
      this.canvas.addEventListener('pointerdown', show);
      this.canvas.addEventListener('pointerleave', () => { tip.hidden = true; });
      this.canvas.addEventListener('click', () => { const t = this.blips.find((b) => !tip.hidden && tip.firstChild && tip.firstChild.textContent === (b.symbol || b.mint.slice(0, 6))); if (t) D.emit('open-coin', t.mint, t.symbol); });
      D.on('theme', () => { this.dirty = true; });
      D.ticker((t, dt) => this.tick(t, dt));
    },

    set(rows) {
      const seen = new Set();
      this.blips = rows.filter((r) => (seen.has(r.mint) ? false : (seen.add(r.mint), true))).slice(0, 60).map((r) => ({ ...r, angle: D.hash(r.mint) * Math.PI * 2 }));
      this.dirty = true;
    },

    tick(t, dt) {
      if (!this.canvas || !this.canvas.offsetParent || !this.visible) return;
      if (!D.reduced()) this.sweep = (this.sweep + (dt / 6000) * Math.PI * 2) % (Math.PI * 2);
      else if (!this.dirty) return;
      this.dirty = false;
      const { ctx, w, h } = D.fit(this.canvas), cx = w / 2, cy = h / 2, R0 = Math.min(w, h) / 2 - 8;
      ctx.clearRect(0, 0, w, h);
      ctx.strokeStyle = D.css('--hair-strong'); ctx.lineWidth = 1;
      for (const k of [0.25, 0.5, 0.75, 1]) { ctx.globalAlpha = k === 1 ? 1 : 0.5; ctx.beginPath(); ctx.arc(cx, cy, R0 * k, 0, 6.3); ctx.stroke(); }
      ctx.globalAlpha = 0.4; for (let i = 0; i < 6; i++) { const a = (i * Math.PI) / 3; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(a) * R0, cy + Math.sin(a) * R0); ctx.stroke(); } ctx.globalAlpha = 1;
      if (!D.reduced()) {   // the sweep
        const g = ctx.createConicGradient ? ctx.createConicGradient(this.sweep - 0.9, cx, cy) : null;
        if (g) { const c = D.css('--green'); g.addColorStop(0, c + '00'); g.addColorStop(0.14, c + '38'); g.addColorStop(0.15, c + '00'); g.addColorStop(1, c + '00'); ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, R0, 0, 6.3); ctx.fill(); }
        ctx.strokeStyle = D.css('--green'); ctx.globalAlpha = 0.5; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(this.sweep) * R0, cy + Math.sin(this.sweep) * R0); ctx.stroke(); ctx.globalAlpha = 1;
      }
      const now = Date.now() / 1000;
      for (const b of this.blips) {
        const age = Math.min(1, (now - b.ts) / 5400), rad = R0 * (0.14 + 0.84 * age);
        b.px = cx + Math.cos(b.angle) * rad; b.py = cy + Math.sin(b.angle) * rad;
        const diff = ((this.sweep - b.angle) % 6.2832 + 6.2832) % 6.2832, glow = D.reduced() ? 0.4 : Math.max(0, 1 - diff / 1.6);
        const col = b.decision === 'pass' ? D.css('--green-hi') : D.css('--red-hi');
        ctx.globalAlpha = Math.max(0.28, 1 - age * 0.7) * (0.55 + 0.45 * glow); ctx.fillStyle = col; ctx.shadowColor = col; ctx.shadowBlur = 4 + 12 * glow;
        ctx.beginPath(); ctx.arc(b.px, b.py, b.decision === 'pass' ? 4.5 : 3.2, 0, 6.3); ctx.fill(); ctx.shadowBlur = 0; ctx.globalAlpha = 1;
      }
    },
  });
})();
