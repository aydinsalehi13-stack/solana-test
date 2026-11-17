/* A rolling number: each digit is a column of 0-9 that scrolls to its value (600 ms, 30 ms stagger, least significant first). */
(function () {
  'use strict';
  const D = window.D;

  class Odometer {
    constructor(el, format) {
      this.el = el;
      this.format = format || ((n) => D.usd(n));
      this.sig = null;
      this.cols = [];
      this.last = undefined;
    }

    set(value) {
      if (value === this.last) return;
      this.last = value;
      if (!D.isNum(value)) {
        this.el.textContent = 'no data';
        this.el.setAttribute('aria-label', 'no data');
        this.el.classList.add('od--none');
        this.sig = null;
        return;
      }
      this.el.classList.remove('od--none');
      const text = this.format(value);
      this.el.setAttribute('aria-label', text);
      const sig = text.replace(/[0-9]/g, '#');
      if (sig !== this.sig) this.build(text, sig);  // the shape changed (a new digit, a comma): rebuild without rolling
      else this.roll(text);
    }

    build(text, sig) {
      this.sig = sig;
      this.cols = [];
      this.el.textContent = '';
      for (const ch of text) {
        if (/[0-9]/.test(ch)) {
          const strip = D.h('span', null, ...Array.from({ length: 10 }, (_, i) => D.h('i', { text: String(i) })));
          strip.style.transition = 'none';
          strip.style.transform = 'translateY(' + -Number(ch) + 'em)';
          const col = D.h('span', { class: 'od__d', 'aria-hidden': 'true' }, strip);
          this.el.append(col);
          this.cols.push(strip);
        } else {
          this.el.append(D.h('span', { class: 'od__s', 'aria-hidden': 'true', text: ch }));
        }
      }
      requestAnimationFrame(() => this.cols.forEach((s) => { s.style.transition = ''; }));
    }

    roll(text) {
      const digits = text.replace(/[^0-9]/g, '');
      const n = this.cols.length;
      this.cols.forEach((strip, i) => {
        strip.style.transitionDelay = D.reduced() ? '0ms' : (n - 1 - i) * 30 + 'ms';
        strip.style.transform = 'translateY(' + -Number(digits[i]) + 'em)';
      });
    }
  }
  D.Odometer = Odometer;
})();
