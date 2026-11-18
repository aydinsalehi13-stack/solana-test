/* Hand-drawn charts on <canvas>/SVG: the value line, candles, sparkline, ring. Thin marks, no chart junk, one axis. */
(function () {
  'use strict';
  const D = window.D;

  const colorOf = (name) => D.css(name === 'prompt' ? '--prompt' : '--orion');
  const ease = (k) => 1 - Math.pow(1 - Math.min(1, Math.max(0, k)), 3);

  /* ---------------------------------- value chart ----------------------------------
     One axis: dollars above or below what was put in. The bold line is the whole desk (both strategies added up), the dashed
     line is the money put in, and each strategy is a thin line of its own. The head dot is where the desk is right now. */
  const EQ = (D.equity = {
    canvas: null, fx: null, stage: null, tip: null, data: { series: [], markers: [], now: 0, window: null }, model: null, hover: null, layout: null, dirty: true,
    reveal: null, head: null, lastKey: '',

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

    set(payload) {
      this.data = payload;
      this.model = this.build(payload);
      const has = !!this.model;
      D.$('#chart-empty').hidden = has;
      const key = payload.range + '|' + has;
      if (has && key !== this.lastKey && !D.reduced()) this.reveal = { t0: performance.now(), ms: 950 };   // a new view draws itself in, left to right
      this.lastKey = key;
      const m = this.model;
      D.$('#chart-t0').textContent = m ? D.hm(m.t0) : '';
      D.$('#chart-t1').textContent = m ? D.hm(m.t1) : '';
      this.dirty = true;
    },

    /* Turn the two balance series into dollars above/below start, plus their sum on a shared clock. */
    build(p) {
      const live = p.series.filter((s) => s.points.length > 0);
      if (!live.some((s) => s.points.length > 1)) return null;
      const times = Array.from(new Set(live.flatMap((s) => s.points.map((q) => q[0])))).sort((a, b) => a - b);
      const idx = live.map(() => 0);
      const total = times.map((t) => {
        let sum = 0;
        live.forEach((s, k) => {
          while (idx[k] + 1 < s.points.length && s.points[idx[k] + 1][0] <= t) idx[k]++;
          sum += s.points[idx[k]][1] - s.start_cash;   // before a strategy's first reading its first reading stands in
        });
        return [t, sum];
      });
      const lines = live.map((s) => ({ name: s.name, start: s.start_cash, points: s.points.map(([t, v]) => [t, v - s.start_cash]) }));
      const putIn = live.reduce((a, s) => a + s.start_cash, 0);
      let lo = 0, hi = 0;
      for (const [, v] of total) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      for (const l of lines) for (const [, v] of l.points) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
      if (hi - lo < 1) { lo -= 0.5; hi += 0.5; }
      const pad = (hi - lo) * 0.16;
      const w = p.window;
      const t0 = w ? Math.min(w[0], times[0]) : times[0];
      let t1 = w ? w[1] : times[times.length - 1];
      if (t1 <= t0) t1 = t0 + 60;
      return { total, lines, putIn, lo: lo - pad, hi: hi + pad, t0, t1 };
    },

    tick(t) {
      if (!this.canvas || !this.stage.offsetParent) return;
      if (this.reveal) this.dirty = true;
      if (this.dirty) { this.drawMain(); this.dirty = false; }
      this.drawFx(t);
    },

    progress() {
      if (!this.reveal) return 1;
      const k = (performance.now() - this.reveal.t0) / this.reveal.ms;
      if (k >= 1) { this.reveal = null; return 1; }
      return ease(k);
    },

    drawMain() {
      const { ctx, w, h } = D.fit(this.canvas);
      ctx.clearRect(0, 0, w, h);
      const m = this.model;
      if (!m) { this.layout = null; return; }
      const pad = { t: 14, b: 6 };
      const X = (t) => ((t - m.t0) / (m.t1 - m.t0)) * w;
      const Y = (v) => pad.t + (1 - (v - m.lo) / (m.hi - m.lo)) * (h - pad.t - pad.b);
      this.layout = { X, Y, w, h, pad };
      const prog = this.progress();
      const last = m.total[m.total.length - 1];
      const up = last[1] >= -0.005;
      const main = D.css(up ? '--green-hi' : '--red-hi');
      // what was put in: one dashed line across the whole session
      const y0 = Math.round(Y(0)) + 0.5;
      ctx.setLineDash([3, 5]); ctx.strokeStyle = D.css('--hair-strong'); ctx.lineWidth = 1.2; ctx.beginPath(); ctx.moveTo(0, y0); ctx.lineTo(w, y0); ctx.stroke(); ctx.setLineDash([]);
      const xEnd = X(last[0]);
      ctx.save();
      ctx.beginPath(); ctx.rect(0, 0, Math.max(1, xEnd * prog + 1), h); ctx.clip();
      // each strategy: a thin line of its own (wide charts only: a phone shows the one desk line, like the reference)
      for (const l of m.lines) {
        if (l.points.length < 2 || w < 560) continue;
        ctx.beginPath(); l.points.forEach(([t, v], i) => (i ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))));
        ctx.lineJoin = 'round'; ctx.lineWidth = 1.3; ctx.globalAlpha = 0.75; ctx.strokeStyle = colorOf(l.name); ctx.stroke(); ctx.globalAlpha = 1;
      }
      // the desk: soft fill down to the bottom edge, then the line
      const g = ctx.createLinearGradient(0, pad.t, 0, h);
      g.addColorStop(0, main + '38'); g.addColorStop(1, main + '05');
      ctx.beginPath(); m.total.forEach(([t, v], i) => (i ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))));
      ctx.lineTo(xEnd, h); ctx.lineTo(X(m.total[0][0]), h); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
      ctx.beginPath(); m.total.forEach(([t, v], i) => (i ? ctx.lineTo(X(t), Y(v)) : ctx.moveTo(X(t), Y(v))));
      ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.lineWidth = 2.6; ctx.strokeStyle = main; ctx.stroke();
      // closed trades: a ring on the line, and a +$/-$ pill on the newest few
      const mk = this.data.markers.filter((x) => x.ts >= m.t0 && x.ts <= m.t1);
      const pills = [], surface = D.css('--glass'), small = w < 480;
      ctx.font = '600 11px ' + D.css('--font-mono');
      mk.forEach((mark, i) => {
        const p = nearest(m.total, mark.ts), x = X(mark.ts), y = Y(p[1]), col = D.css(mark.pnl >= 0 ? '--green-hi' : '--red-hi');
        ctx.fillStyle = surface; ctx.strokeStyle = col; ctx.lineWidth = 1.8; ctx.beginPath(); ctx.arc(x, y, 3.2, 0, 6.3); ctx.fill(); ctx.stroke();
        if (i < mk.length - (small ? 1 : 4) || Math.abs(x - xEnd) < 34) return;   // never under the head dot
        const txt = D.usd(mark.pnl, { sign: true }), tw = ctx.measureText(txt).width + 14;
        const px = Math.min(w - tw / 2 - 4, Math.max(tw / 2 + 4, x)), py = y - 18 < pad.t ? y + 18 : y - 18;
        if (pills.some((q) => Math.abs(q - px) < tw + 4)) return;
        pills.push(px);
        ctx.fillStyle = surface; ctx.strokeStyle = D.css('--hair-strong'); ctx.lineWidth = 1; roundRect(ctx, px - tw / 2, py - 9.5, tw, 19, 9.5); ctx.fill(); ctx.stroke();
        ctx.fillStyle = col; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(txt, px, py + 0.5);
      });
      ctx.restore();
    },

    drawFx(t) {
      const { ctx, w, h } = D.fit(this.fx);
      ctx.clearRect(0, 0, w, h);
      const L = this.layout, m = this.model;
      if (!L || !m) return;
      // the head: where the desk is now. It rides the line while the chart draws in, then glides to each new reading.
      const last = m.total[m.total.length - 1];
      const prog = this.progress();
      let target;
      if (prog < 1) { const tt = m.total[0][0] + (last[0] - m.total[0][0]) * prog; target = { x: L.X(tt), y: L.Y(valueAt(m.total, tt)) }; this.head = target; }
      else {
        target = { x: L.X(last[0]), y: L.Y(last[1]) };
        if (!this.head || D.reduced()) this.head = target;
        else { this.head = { x: this.head.x + (target.x - this.head.x) * 0.2, y: this.head.y + (target.y - this.head.y) * 0.2 }; }
      }
      const hx = Math.min(w - 8, Math.max(8, this.head.x)), hy = this.head.y;
      const col = D.css(last[1] >= -0.005 ? '--green-hi' : '--red-hi');
      const phase = D.reduced() ? 0.35 : (t % 2000) / 2000;
      ctx.strokeStyle = col; ctx.globalAlpha = (1 - phase) * 0.7; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(hx, hy, 6 + phase * 12, 0, 6.3); ctx.stroke(); ctx.globalAlpha = 1;
      ctx.fillStyle = D.css('--glass'); ctx.strokeStyle = D.css('--ink'); ctx.lineWidth = 2.6; ctx.beginPath(); ctx.arc(hx, hy, 5.6, 0, 6.3); ctx.fill(); ctx.stroke();
      // crosshair + tooltip on hover / touch
      if (this.hover) {
        const cx = Math.min(w, Math.max(0, this.hover.x)), tt = Math.min(last[0], Math.max(m.total[0][0], m.t0 + (cx / w) * (m.t1 - m.t0)));
        const x = L.X(tt);
        ctx.strokeStyle = D.css('--hair-strong'); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(Math.round(x) + 0.5, 0); ctx.lineTo(Math.round(x) + 0.5, h); ctx.stroke();
        const tv = valueAt(m.total, tt);
        ctx.fillStyle = col; ctx.strokeStyle = D.css('--glass'); ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, L.Y(tv), 4.5, 0, 6.3); ctx.fill(); ctx.stroke();
        const tip = this.tip;
        tip.replaceChildren(D.h('small', { text: new Date(tt * 1000).toLocaleString([], { hour: '2-digit', minute: '2-digit', month: 'short', day: 'numeric' }) }),
          D.h('div', null, D.h('i', { style: 'background:' + col }), 'value  ', D.h('b', { text: D.usd(m.putIn + tv) }), '  ', D.h('span', { class: D.dir(tv), text: D.usd(tv, { sign: true }) })),
          ...m.lines.map((l) => { const v = valueAt(l.points, tt); return D.h('div', null, D.h('i', { style: 'background:' + colorOf(l.name) }), l.name + '  ', D.h('b', { text: D.usd(l.start + v) }), '  ', D.h('span', { class: D.dir(v), text: D.usd(v, { sign: true }) })); }));
        tip.hidden = false;
        const tw = tip.offsetWidth;
        tip.style.left = Math.min(w - tw - 6, Math.max(6, x + 14)) + 'px'; tip.style.top = '4px';
      }
    },
  });

  /* the last reading at or before t (a balance holds until the next one) */
  function valueAt(points, t) {
    let lo = 0, hi = points.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (points[mid][0] <= t) lo = mid; else hi = mid - 1; }
    const a = points[lo], b = points[Math.min(points.length - 1, lo + 1)];
    if (b[0] === a[0] || t <= a[0]) return a[1];
    return a[1] + (b[1] - a[1]) * Math.min(1, (t - a[0]) / (b[0] - a[0]));   // drawn as a straight segment, so read it the same way
  }

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
