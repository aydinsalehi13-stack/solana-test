/* Hand-drawn charts on <canvas>/SVG: equity (two lines), candles, sparkline, ring. Thin marks, recessive grid, one axis. */
(function () {
  'use strict';
  const D = window.D;

  const colorOf = (name) => D.css(name === 'prompt' ? '--prompt' : '--orion');
  const LABEL = { prompt: 'prompt', orion_fast: 'orion_fast' };

  /* ---------------------------------- equity chart ---------------------------------- */
  const EQ = (D.equity = {
    canvas: null, fx: null, stage: null, tip: null, data: { series: [], markers: [], now: 0 }, hover: null, layout: null, dirty: true,

    init() {
      this.canvas = D.$('#equity'); this.fx = D.$('#equity-fx'); this.stage = D.$('#chart-stage'); this.tip = D.$('#chart-tip');
      new ResizeObserver(() => { this.dirty = true; }).observe(this.stage);
      const move = (e) => { const r = this.stage.getBoundingClientRect(); this.hover = { x: (e.touches ? e.touches[0].clientX : e.clientX) - r.left }; this.dirty = true; };
      this.stage.addEventListener('pointermove', move);
      this.stage.addEventListener('pointerdown', move);
      this.stage.addEventListener('pointerleave', () => { this.hover = null; this.tip.hidden = true; this.dirty = true; });
      D.on('theme', () => { this.dirty = true; });
      D.ticker((t) => this.tick(t));
    },

    set(payload) { this.data = payload; this.dirty = true; D.$('#chart-empty').hidden = payload.series.some((s) => s.points.length > 1); },

    tick(t) {
      if (!this.canvas || !this.stage.offsetParent) return;
      if (this.dirty) { this.drawMain(); this.dirty = false; }
      this.drawFx(t);
    },

    bounds() {
      const all = this.data.series.flatMap((s) => s.points);
      if (all.length < 2) return null;
      let t0 = Infinity, t1 = -Infinity, lo = Infinity, hi = -Infinity;
      for (const [t, v] of all) { t0 = Math.min(t0, t); t1 = Math.max(t1, t); lo = Math.min(lo, v); hi = Math.max(hi, v); }
      for (const s of this.data.series) if (s.points.length) { lo = Math.min(lo, s.start_cash); hi = Math.max(hi, s.start_cash); }
      if (hi - lo < 1) { const m = (hi + lo) / 2; lo = m - 0.5; hi = m + 0.5; }
      const pad = (hi - lo) * 0.12;
      return { t0, t1: t1 === t0 ? t0 + 60 : t1, lo: lo - pad, hi: hi + pad };
    },

    drawMain() {
      const { ctx, w, h } = D.fit(this.canvas);
      ctx.clearRect(0, 0, w, h);
      const b = this.bounds();
      if (!b) { this.layout = null; return; }
      const mobile = w < 480;
      const m = { l: mobile ? 44 : 52, r: mobile ? 12 : 84, t: 10, b: 22 };
      const X = (t) => m.l + ((t - b.t0) / (b.t1 - b.t0)) * (w - m.l - m.r);
      const Y = (v) => m.t + (1 - (v - b.lo) / (b.hi - b.lo)) * (h - m.t - m.b);
      this.layout = { m, X, Y, b, w, h };
      const grid = D.css('--grid'), label = D.css('--muted'), surface = D.css('--glass');
      ctx.font = '11px ' + D.css('--font-ui');
      ctx.textBaseline = 'middle';
      // recessive grid + y labels
      ctx.lineWidth = 1;
      for (let i = 0; i <= 3; i++) {
        const v = b.lo + ((b.hi - b.lo) * i) / 3, y = Math.round(Y(v)) + 0.5;
        ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(w - m.r, y); ctx.stroke();
        ctx.fillStyle = label; ctx.textAlign = 'right'; ctx.fillText('$' + v.toFixed(v > 1000 ? 0 : 1), m.l - 8, y);
      }
      // x labels
      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
      const ticks = mobile ? 3 : 5;
      for (let i = 0; i < ticks; i++) {
        const t = b.t0 + ((b.t1 - b.t0) * i) / (ticks - 1);
        ctx.fillStyle = label; ctx.fillText(D.hm(t), Math.min(w - m.r - 14, Math.max(m.l + 14, X(t))), h - 5);
      }
      // start-cash reference (dashed), once per distinct value
      const seen = new Set();
      for (const s of this.data.series) {
        if (!s.points.length || seen.has(s.start_cash)) continue;
        seen.add(s.start_cash);
        const y = Math.round(Y(s.start_cash)) + 0.5;
        ctx.setLineDash([4, 5]); ctx.strokeStyle = D.css('--hair-strong'); ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(w - m.r, y); ctx.stroke(); ctx.setLineDash([]);
      }
      // series: area, glow line
      for (const s of this.data.series) {
        if (s.points.length < 2) continue;
        const c = colorOf(s.name);
        const g = ctx.createLinearGradient(0, m.t, 0, h - m.b);
        g.addColorStop(0, c + '30'); g.addColorStop(1, c + '00');
        ctx.beginPath();
        s.points.forEach(([t, v], i) => (i ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))));
        const lastP = s.points[s.points.length - 1];
        ctx.lineTo(X(lastP[0]), h - m.b); ctx.lineTo(X(s.points[0][0]), h - m.b); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
        ctx.beginPath();
        s.points.forEach(([t, v], i) => (i ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))));
        ctx.lineJoin = 'round'; ctx.lineWidth = 2; ctx.strokeStyle = c; ctx.shadowColor = c; ctx.shadowBlur = D.reduced() ? 0 : 9; ctx.stroke(); ctx.shadowBlur = 0;
        if (!mobile) {  // direct label at the line end (text in the label ink, a colored mark carries identity)
          ctx.fillStyle = c; ctx.beginPath(); ctx.arc(w - m.r + 12, Y(lastP[1]), 3.5, 0, 6.3); ctx.fill();
          ctx.fillStyle = D.css('--label'); ctx.textAlign = 'left'; ctx.textBaseline = 'middle'; ctx.fillText(LABEL[s.name] || s.name, w - m.r + 20, Y(lastP[1]));
        }
      }
      // trade markers: a ring on the line, and a +$/-$ pill on the most recent few
      const mk = this.data.markers.filter((x) => x.ts >= b.t0 && x.ts <= b.t1);
      const pills = [];   // x of the pills already drawn: a pill that would sit on another one is skipped
      mk.forEach((mark, idx) => {
        const s = this.data.series.find((q) => q.name === mark.strategy);
        if (!s || !s.points.length) return;
        const p = nearest(s.points, mark.ts);
        const x = X(mark.ts), y = Y(p[1]);
        ctx.fillStyle = surface; ctx.strokeStyle = mark.pnl >= 0 ? D.css('--green-hi') : D.css('--red-hi'); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, 3.6, 0, 6.3); ctx.fill(); ctx.stroke();
        if (idx >= mk.length - (mobile ? 3 : 5)) {
          const txt = D.usd(mark.pnl, { sign: true, d: 2 }); const tw = ctx.measureText(txt).width + 12;
          const px = Math.min(w - m.r - tw / 2, Math.max(m.l + tw / 2, x)), py = y - 17 < m.t + 8 ? y + 17 : y - 17;
          if (pills.some((q) => Math.abs(q - px) < tw + 4)) return;
          pills.push(px);
          ctx.fillStyle = surface; ctx.strokeStyle = D.css('--hair-strong'); ctx.lineWidth = 1;
          roundRect(ctx, px - tw / 2, py - 9, tw, 18, 9); ctx.fill(); ctx.stroke();
          ctx.fillStyle = mark.pnl >= 0 ? D.css('--green-hi') : D.css('--red-hi'); ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(txt, px, py + 0.5);
        }
      });
    },

    drawFx(t) {
      const { ctx, w, h } = D.fit(this.fx);
      ctx.clearRect(0, 0, w, h);
      const L = this.layout;
      if (!L) return;
      // live dot at the end of each line (a slow ring: the line is live while the desk is)
      const phase = D.reduced() ? 0.4 : (t % 2200) / 2200;
      for (const s of this.data.series) {
        if (!s.points.length) continue;
        const [pt, pv] = s.points[s.points.length - 1], c = colorOf(s.name), x = L.X(pt), y = L.Y(pv);
        ctx.fillStyle = c; ctx.beginPath(); ctx.arc(x, y, 4, 0, 6.3); ctx.fill();
        ctx.strokeStyle = c; ctx.globalAlpha = 1 - phase; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, 4 + phase * 9, 0, 6.3); ctx.stroke(); ctx.globalAlpha = 1;
      }
      // crosshair + tooltip on hover / touch
      if (this.hover) {
        const { m } = L, hx = Math.min(w - m.r, Math.max(m.l, this.hover.x)), tt = L.b.t0 + ((hx - m.l) / (w - m.l - m.r)) * (L.b.t1 - L.b.t0);
        ctx.strokeStyle = D.css('--hair-strong'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(Math.round(hx) + 0.5, m.t); ctx.lineTo(Math.round(hx) + 0.5, h - m.b); ctx.stroke();
        const rows = [];
        for (const s of this.data.series) {
          if (!s.points.length) continue;
          const p = nearest(s.points, tt);
          ctx.fillStyle = colorOf(s.name); ctx.strokeStyle = D.css('--glass'); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(L.X(p[0]), L.Y(p[1]), 4.5, 0, 6.3); ctx.fill(); ctx.stroke();
          rows.push([s.name, p[1], s.start_cash]);
        }
        const tip = this.tip;
        tip.replaceChildren(D.h('small', { text: new Date(tt * 1000).toLocaleString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', month: 'short', day: 'numeric' }) }),
          ...rows.map(([n, v, st]) => D.h('div', null, D.h('i', { class: 'swatch swatch--' + (n === 'prompt' ? 'prompt' : 'orion'), style: 'display:inline-block;margin-right:7px' }),
            n + '  ', D.h('b', { text: D.usd(v) }), '  ', D.h('span', { class: D.dir(v - st), text: D.pct(100 * (v - st) / st, { sign: true }) }))));
        tip.hidden = false;
        const tw = tip.offsetWidth;
        tip.style.left = Math.min(w - tw - 4, Math.max(4, hx + 14)) + 'px'; tip.style.top = '6px';
      }
    },
  });

  function nearest(points, t) {
    let lo = 0, hi = points.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (points[mid][0] < t) lo = mid + 1; else hi = mid; }
    const a = points[Math.max(0, lo - 1)], b = points[lo];
    return Math.abs(a[0] - t) <= Math.abs(b[0] - t) ? a : b;
  }
  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }

  /* ---------------------------------- one-minute candles ---------------------------------- */
  D.drawCandles = (canvas, candles, position) => {
    const { ctx, w, h } = D.fit(canvas);
    ctx.clearRect(0, 0, w, h);
    if (!candles || candles.length < 2) return false;
    const m = { l: 6, r: 58, t: 8, b: 20 };
    let lo = Math.min(...candles.map((c) => c[3])), hi = Math.max(...candles.map((c) => c[2]));
    if (position) { lo = Math.min(lo, position.entry_price || lo, position.stop_price || lo); hi = Math.max(hi, position.entry_price || hi); }
    const pad = (hi - lo) * 0.08 || hi * 0.02; lo -= pad; hi += pad;
    const n = candles.length, step = (w - m.l - m.r) / n, bw = Math.max(1.5, Math.min(10, step * 0.62));
    const Y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * (h - m.t - m.b);
    ctx.font = '11px ' + D.css('--font-ui'); ctx.textBaseline = 'middle';
    for (let i = 0; i < 3; i++) { const v = lo + ((hi - lo) * i) / 2, y = Math.round(Y(v)) + 0.5; ctx.strokeStyle = D.css('--grid'); ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(w - m.r, y); ctx.stroke(); ctx.fillStyle = D.css('--muted'); ctx.textAlign = 'left'; ctx.fillText(D.price(v), w - m.r + 6, y); }
    candles.forEach((c, i) => {
      const x = m.l + step * (i + 0.5), up = c[4] >= c[1], col = up ? D.css('--green-hi') : D.css('--red-hi');
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, Y(c[2])); ctx.lineTo(Math.round(x) + 0.5, Y(c[3])); ctx.stroke();
      const y1 = Y(Math.max(c[1], c[4])), y2 = Y(Math.min(c[1], c[4]));
      ctx.fillRect(x - bw / 2, y1, bw, Math.max(1.5, y2 - y1));
    });
    const line = (v, col, text) => {
      if (!v) return; const y = Math.round(Y(v)) + 0.5;
      ctx.setLineDash([5, 4]); ctx.strokeStyle = col; ctx.beginPath(); ctx.moveTo(m.l, y); ctx.lineTo(w - m.r, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = col; ctx.textAlign = 'right'; ctx.fillText(text, w - m.r - 4, y - 8);
    };
    if (position) { line(position.entry_price, D.css('--blue'), 'entry'); line(position.stop_price, D.css('--red-hi'), 'stop'); }
    ctx.fillStyle = D.css('--muted'); ctx.textAlign = 'left'; ctx.textBaseline = 'alphabetic';
    ctx.fillText(D.hm(candles[0][0]), m.l, h - 5); ctx.textAlign = 'right'; ctx.fillText(D.hm(candles[n - 1][0]), w - m.r, h - 5);
    return true;
  };

  /* ---------------------------------- sparkline + ring (SVG) ---------------------------------- */
  D.spark = (values, color) => {
    const w = 64, h = 24, max = Math.max(1, ...values), n = values.length;
    const pts = values.map((v, i) => [(i / (n - 1)) * (w - 2) + 1, h - 3 - (v / max) * (h - 7)]);
    const svg = D.svg('svg', { class: 'spark', viewBox: `0 0 ${w} ${h}`, 'aria-hidden': 'true' });
    const flat = values.every((v) => v === 0);
    svg.append(D.svg('polyline', { points: pts.map((p) => p.join(',')).join(' '), fill: 'none', stroke: color, 'stroke-width': 1.6, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'stroke-dasharray': flat ? '1 4' : null, opacity: flat ? 0.5 : 1 }));
    if (!flat) svg.append(D.svg('circle', { cx: pts[n - 1][0], cy: pts[n - 1][1], r: 2.2, fill: color }));
    return svg;
  };
  D.ring = (pct, color) => {
    const r = 12, c = 2 * Math.PI * r;
    const svg = D.svg('svg', { class: 'ring', viewBox: '0 0 30 30', role: 'img', 'aria-label': pct == null ? 'no pass rate yet' : Math.round(pct) + ' percent of recent decisions passed' });
    svg.append(D.svg('circle', { cx: 15, cy: 15, r, fill: 'none', stroke: 'var(--hair-strong)', 'stroke-width': 3 }));
    svg.append(D.svg('circle', { class: 'ring__v', cx: 15, cy: 15, r, fill: 'none', stroke: color, 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-dasharray': c, 'stroke-dashoffset': c * (1 - (pct || 0) / 100), transform: 'rotate(-90 15 15)', style: 'transition: stroke-dashoffset .7s var(--ease)' }));
    svg.append(D.svg('text', { x: 15, y: 17.8, 'text-anchor': 'middle' }, pct == null ? '–' : String(Math.round(pct))));
    return svg;
  };
})();
