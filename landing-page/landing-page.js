/* ============================================================
   ZENITH — landing interactions + SVG data viz
   ============================================================ */
(function () {
  document.documentElement.classList.add('js');
  const NS = 'http://www.w3.org/2000/svg';
  const el = (t, a) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]); return e; };

  /* deterministic rng */
  function rng(seed) { let s = seed; return () => { s = (s * 9301 + 49297) % 233280; return s / 233280; }; }

  /* ---- equity curve: $10k -> $135k, ~76 months, log-ish compounding with drawdowns ---- */
  function buildEquity() {
    const r = rng(7);
    const months = 76; // Jan 2020 -> Apr 2026
    const start = 10000, end = 135000;
    const pts = [];
    // exponential base path in log space + noise + periodic drawdowns
    const logStart = Math.log(start), logEnd = Math.log(end);
    let prev = logStart;
    for (let i = 0; i <= months; i++) {
      const t = i / months;
      // ease so growth accelerates slightly later (compounding)
      const base = logStart + (logEnd - logStart) * (0.35 * t + 0.65 * t * t);
      let noise = (r() - 0.5) * 0.05;
      // inject a few drawdowns
      const ddPhase = Math.sin(t * Math.PI * 5);
      if (ddPhase < -0.85) noise -= 0.06;
      let v = base + noise;
      // smooth
      v = prev * 0.25 + v * 0.75;
      prev = v;
      pts.push(Math.exp(v));
    }
    pts[0] = start; pts[pts.length - 1] = end;
    return pts;
  }
  const EQUITY = buildEquity();

  /* generic area line chart */
  function areaChart(svg, data, opt) {
    const o = Object.assign({ pad: 0, stroke: '#00C896', sw: 2, fill: true, glow: false, dashGrid: false }, opt);
    const W = opt.w, H = opt.h;
    const pad = o.pad;
    const min = Math.min(...data), max = Math.max(...data);
    const x = i => pad + (i / (data.length - 1)) * (W - pad * 2);
    const y = v => pad + (H - pad * 2) - ((v - min) / (max - min)) * (H - pad * 2);
    let d = '';
    data.forEach((v, i) => { d += `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)} `; });
    const gid = 'g' + Math.random().toString(36).slice(2, 7);
    if (o.fill) {
      const defs = el('defs', {});
      const grad = el('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
      grad.appendChild(el('stop', { offset: '0%', 'stop-color': o.stroke, 'stop-opacity': '0.28' }));
      grad.appendChild(el('stop', { offset: '100%', 'stop-color': o.stroke, 'stop-opacity': '0' }));
      defs.appendChild(grad); svg.appendChild(defs);
      svg.appendChild(el('path', { d: `${d} L${x(data.length - 1)},${H - pad} L${x(0)},${H - pad} Z`, fill: `url(#${gid})` }));
    }
    const path = el('path', { d: d.trim(), fill: 'none', stroke: o.stroke, 'stroke-width': o.sw, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' });
    if (o.glow) path.setAttribute('filter', `drop-shadow(0 0 6px ${o.stroke}88)`);
    svg.appendChild(path);
    return { x, y };
  }

  /* ---- hero background curve (wide, faint) ---- */
  function heroCurve() {
    const host = document.getElementById('heroCurve');
    if (!host) return;
    const W = 1440, H = 380;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H, preserveAspectRatio: 'none' });
    areaChart(svg, EQUITY, { w: W, h: H, pad: 0, stroke: '#00C896', sw: 2.5, fill: true, glow: true });
    host.appendChild(svg);
  }

  /* ---- hero terminal mini chart ---- */
  function termChart() {
    const host = document.getElementById('termChart');
    if (!host) return;
    const W = 300, H = 92;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H });
    const slice = EQUITY.slice(-26);
    areaChart(svg, slice, { w: W, h: H, pad: 4, stroke: '#00C896', sw: 2, fill: true });
    host.appendChild(svg);
  }

  /* ---- performance equity curve (with axes labels) ---- */
  function perfChart() {
    const host = document.getElementById('perfChart');
    if (!host) return;
    const W = 680, H = 300, padL = 48, padB = 24, padT = 12, padR = 8;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: 'auto' });
    const data = EQUITY;
    const min = Math.min(...data), max = Math.max(...data);
    const x = i => padL + (i / (data.length - 1)) * (W - padL - padR);
    const y = v => padT + (H - padT - padB) - ((v - min) / (max - min)) * (H - padT - padB);
    // gridlines + $ labels (log-ish ticks)
    const ticks = [10000, 35000, 70000, 105000, 135000];
    ticks.forEach(tk => {
      const yy = y(tk);
      svg.appendChild(el('line', { x1: padL, y1: yy, x2: W - padR, y2: yy, stroke: '#1c222b', 'stroke-width': 1 }));
      const t = el('text', { x: padL - 8, y: yy + 3, 'text-anchor': 'end', 'font-size': 10, fill: '#79808B', 'font-family': 'JetBrains Mono' });
      t.textContent = '$' + (tk / 1000) + 'k'; svg.appendChild(t);
    });
    // year labels
    const yearStarts = [['2020', 0], ['2021', 12], ['2022', 24], ['2023', 36], ['2024', 48], ['2025', 60], ['2026', 72]];
    yearStarts.forEach(([lbl, i]) => {
      const t = el('text', { x: x(i), y: H - 7, 'text-anchor': 'middle', 'font-size': 10, fill: '#79808B', 'font-family': 'JetBrains Mono' });
      t.textContent = lbl; svg.appendChild(t);
    });
    // aligned area + line
    const gid = 'perfg';
    const defs = el('defs', {});
    const grad = el('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
    grad.appendChild(el('stop', { offset: '0%', 'stop-color': '#00C896', 'stop-opacity': '0.26' }));
    grad.appendChild(el('stop', { offset: '100%', 'stop-color': '#00C896', 'stop-opacity': '0' }));
    defs.appendChild(grad); svg.appendChild(defs);
    let dp = '';
    data.forEach((v, i) => { dp += `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)} `; });
    svg.appendChild(el('path', { d: `${dp} L${x(data.length - 1)},${y(min)} L${x(0)},${y(min)} Z`, fill: `url(#${gid})` }));
    svg.appendChild(el('path', { d: dp.trim(), fill: 'none', stroke: '#00C896', 'stroke-width': 2.2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
    host.appendChild(svg);
  }

  /* ---- AMD FVG step diagrams: one coherent candlestick story, each panel a window ---- */
  function buildStory() {
    const r = rng(42);
    const lvl = 100, cl = [];
    for (let i = 0; i < 10; i++) cl.push(lvl + (r() - 0.5) * 2.6);              // 0-9 accumulation coil
    cl.push(96.7); cl.push(95.0);                                              // 10-11 manipulation sweep down
    cl.push(99.4);                                                            // 12 reclaim (entry)
    cl.push(102.7); cl.push(105.3); cl.push(106.8);                           // 13-15 displacement up (FVG)
    cl.push(105.6); cl.push(106.6);                                           // 16-17 shallow pullback
    cl.push(108.2); cl.push(109.7); cl.push(111.1); cl.push(112.8); cl.push(114.2); cl.push(115.6); // 18-23 trend to TP
    const r2 = rng(7);
    const k = cl.map((c, i) => {
      const o = i === 0 ? c - 0.6 : cl[i - 1];
      const wt = 0.35 + r2() * 0.85, wb = 0.35 + r2() * 0.85;
      return { o, c, h: Math.max(o, c) + wt, l: Math.min(o, c) - wb, up: c >= o };
    });
    // emphasise the sweep candle (long lower wick piercing support)
    k[10].l = 95.0;
    k[11] = { o: 96.6, c: 96.1, h: 96.9, l: 92.5, up: false };
    return k;
  }
  const STORY = buildStory();

  // accumulation range bounds (candles 0-9)
  let ACC_HI = -Infinity, ACC_LO = Infinity;
  for (let i = 0; i < 10; i++) { ACC_HI = Math.max(ACC_HI, STORY[i].h); ACC_LO = Math.min(ACC_LO, STORY[i].l); }
  const ENTRY = 99.4, SL = 93.2, TP = 115.6;

  function drawCandles(svg, slice, W, H, pad, extra) {
    let min = Infinity, max = -Infinity;
    slice.forEach(k => { min = Math.min(min, k.l); max = Math.max(max, k.h); });
    (extra || []).forEach(p => { min = Math.min(min, p); max = Math.max(max, p); });
    const span = (max - min) || 1; min -= span * 0.08; max += span * 0.08;
    const stepX = (W - pad.x * 2) / slice.length;
    const cw = Math.max(3.5, stepX * 0.56);
    const x = i => pad.x + stepX * i + stepX / 2;
    const y = p => pad.t + (H - pad.t - pad.b) * (1 - (p - min) / (max - min));
    const paint = () => slice.forEach((k, i) => {
      const col = k.up ? '#00C896' : '#FF5C5C';
      const cx = x(i);
      svg.appendChild(el('line', { x1: cx.toFixed(1), y1: y(k.h).toFixed(1), x2: cx.toFixed(1), y2: y(k.l).toFixed(1), stroke: col, 'stroke-width': 1 }));
      const yo = y(k.o), yc = y(k.c), top = Math.min(yo, yc), bh = Math.max(1.6, Math.abs(yc - yo));
      svg.appendChild(el('rect', { x: (cx - cw / 2).toFixed(1), y: top.toFixed(1), width: cw.toFixed(1), height: bh.toFixed(1), fill: col, rx: 0.6 }));
    });
    return { x, y, paint, stepX };
  }

  function stepDiagram(id, kind) {
    const host = document.getElementById(id);
    if (!host) return;
    const W = 248, H = 132, pad = { x: 12, t: 13, b: 15 };
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: '100%', preserveAspectRatio: 'none' });

    const win = { acc: [1, 11], man: [5, 16], fvg: [10, 20], entry: [10, 23] }[kind];
    const [a, b] = win;
    const slice = STORY.slice(a, b + 1);
    const extra = kind === 'acc' ? [ACC_HI, ACC_LO]
      : kind === 'man' ? [ACC_HI, ACC_LO]
      : kind === 'entry' ? [ENTRY, SL, TP] : [];
    const ch = drawCandles(svg, slice, W, H, pad, extra);
    const ix = g => g - a;
    const mono = (tx, xx, yy, color, anchor) => { const t = el('text', { x: xx, y: yy, 'font-size': 8.5, fill: color, 'font-family': 'JetBrains Mono', 'font-weight': 600, 'text-anchor': anchor || 'start' }); t.textContent = tx; svg.appendChild(t); };
    const hline = (p, color, op) => svg.appendChild(el('line', { x1: 8, y1: ch.y(p).toFixed(1), x2: W - 8, y2: ch.y(p).toFixed(1), stroke: color, 'stroke-width': 1, 'stroke-dasharray': '4 3', opacity: op == null ? 0.85 : op }));

    // ---- zones (under candles) ----
    if (kind === 'acc' || kind === 'man') {
      const g0 = kind === 'acc' ? 1 : 5;
      const x0 = ch.x(ix(g0)) - ch.stepX * 0.5, x1 = ch.x(ix(9)) + ch.stepX * 0.5;
      svg.appendChild(el('rect', { x: x0.toFixed(1), y: ch.y(ACC_HI).toFixed(1), width: (x1 - x0).toFixed(1), height: (ch.y(ACC_LO) - ch.y(ACC_HI)).toFixed(1), fill: 'rgba(255,255,255,0.04)', stroke: '#6B7280', 'stroke-width': 0.9, 'stroke-dasharray': '3 3', rx: 2 }));
    }
    if (kind === 'fvg') {
      const gapBot = STORY[12].h, gapTop = STORY[14].l;       // bullish FVG gap
      const x0 = ch.x(ix(12)), x1 = ch.x(ix(15));
      svg.appendChild(el('rect', { x: x0.toFixed(1), y: ch.y(gapTop).toFixed(1), width: (x1 - x0).toFixed(1), height: Math.max(7, ch.y(gapBot) - ch.y(gapTop)).toFixed(1), fill: 'rgba(0,200,150,0.16)', stroke: '#00C896', 'stroke-width': 0.8, rx: 1.5 }));
    }
    if (kind === 'entry') {
      svg.appendChild(el('rect', { x: 8, y: ch.y(TP).toFixed(1), width: W - 16, height: (ch.y(ENTRY) - ch.y(TP)).toFixed(1), fill: 'rgba(0,200,150,0.10)' }));
      svg.appendChild(el('rect', { x: 8, y: ch.y(ENTRY).toFixed(1), width: W - 16, height: (ch.y(SL) - ch.y(ENTRY)).toFixed(1), fill: 'rgba(255,77,77,0.10)' }));
    }

    // ---- candles ----
    ch.paint();

    // ---- support line + labels (over candles) ----
    if (kind === 'man') {
      hline(ACC_LO, '#FF5C5C', 0.7);
      mono('sweep', ch.x(ix(11)), (ch.y(STORY[11].l) + 10).toFixed(1), '#FF5C5C', 'middle');
    }
    if (kind === 'fvg') mono('FVG', ch.x(ix(13)), (ch.y(STORY[14].l) - 5).toFixed(1), '#00C896', 'middle');
    if (kind === 'entry') {
      hline(TP, '#00C896', 0.9); mono('TP Hit', W - 9, (ch.y(TP) - 4).toFixed(1), '#00C896', 'end');
      hline(ENTRY, '#4D8DFF', 0.9); mono('entry', W - 9, (ch.y(ENTRY) - 4).toFixed(1), '#4D8DFF', 'end');
      hline(SL, '#FF5C5C', 0.7); mono('SL', W - 9, (ch.y(SL) - 4).toFixed(1), '#FF5C5C', 'end');
      svg.appendChild(el('circle', { cx: ch.x(ix(23)), cy: ch.y(STORY[23].c), r: 2.6, fill: '#00C896' }));
      mono('Long', (ch.x(ix(12)) + 4).toFixed(1), (ch.y(ENTRY) + 11).toFixed(1), '#4D8DFF', 'start');
    }

    host.appendChild(svg);
  }

  /* ---- dashboard preview mockup chart ---- */
  function dashChart() {
    const host = document.getElementById('dashChart');
    if (!host) return;
    const W = 560, H = 150;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: 'auto' });
    areaChart(svg, EQUITY.slice(-40), { w: W, h: H, pad: 6, stroke: '#00C896', sw: 2, fill: true });
    host.appendChild(svg);
  }

  /* ---- yearly returns bars ---- */
  function yearBars() {
    const host = document.getElementById('yearBars');
    if (!host) return;
    host.innerHTML = '<div style="font-size:13px;color:var(--muted);padding:8px 0">Detailed yearly breakdown available in the dashboard after sign-up.</div>';
  }

  /* ---- count-up for stat numbers ---- */
  function countUp(node) {
    const target = parseFloat(node.dataset.val);
    const dec = parseInt(node.dataset.dec || '0', 10);
    const prefix = node.dataset.prefix || '';
    const suffix = node.dataset.suffix || '';
    const dur = 1300; const t0 = performance.now();
    function frame(t) {
      const p = Math.min(1, (t - t0) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      const val = target * e;
      node.textContent = prefix + val.toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }) + suffix;
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  /* ---- init ---- */
  function init() {
    heroCurve(); termChart(); perfChart(); dashChart(); yearBars();
    ['accDiag:acc', 'manDiag:man', 'fvgDiag:fvg', 'entryDiag:entry'].forEach(s => { const [id, k] = s.split(':'); stepDiagram(id, k); });

    // nav scroll state
    const nav = document.querySelector('.nav');
    const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 20);
    window.addEventListener('scroll', onScroll, { passive: true }); onScroll();

    // reveal + counters via scroll position (IntersectionObserver is unreliable in
    // offscreen/embedded render contexts, so check rects directly).
    const revealEls = [...document.querySelectorAll('.reveal'), document.querySelector('.statsbar')].filter(Boolean);
    function activate(t) {
      if (t.classList.contains('in')) return;
      t.classList.add('in');
      t.querySelectorAll('[data-val]').forEach(countUp);
      t.querySelectorAll('.year-fill').forEach(f => { f.style.width = f.dataset.w + '%'; });
    }
    function revealCheck() {
      const trigger = window.innerHeight * 0.92;
      for (const t of revealEls) {
        const r = t.getBoundingClientRect();
        if (r.top < trigger && r.bottom > 0) activate(t);
      }
    }
    window.addEventListener('scroll', revealCheck, { passive: true });
    window.addEventListener('resize', revealCheck);
    revealCheck();
    // safety net: if anything is still hidden shortly after load, reveal it
    setTimeout(() => { revealCheck(); }, 600);

    // demo buttons
    document.querySelectorAll('[data-demo]').forEach(b => b.addEventListener('click', e => {
      e.preventDefault();
      b.textContent = b.dataset.done || 'On the list ✓';
    }));

    // FAQ: single-open accordion (close siblings when one opens)
    const faqItems = [...document.querySelectorAll('.faq-item')];
    faqItems.forEach(item => item.addEventListener('toggle', () => {
      if (item.open) faqItems.forEach(o => { if (o !== item) o.open = false; });
    }));

    // PWA install section: device-aware tabs + native one-click button.
    const installBlock = document.querySelector('.install-block');
    if (installBlock) {
      const standalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

      if (standalone) {
        // Already installed — no reason to advertise installing. Hide the whole
        // section and its nav/footer links.
        const section = document.getElementById('install');
        if (section) section.style.display = 'none';
        document.querySelectorAll('a[href="#install"]').forEach((a) => { a.style.display = 'none'; });
      } else {
        const tabs = [...installBlock.querySelectorAll('.install-tab')];
        const panels = [...installBlock.querySelectorAll('.install-steps')];
        const ua = navigator.userAgent || '';
        const isIOS = /iphone|ipad|ipod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
        const isAndroid = /android/i.test(ua);
        const detected = isIOS ? 'ios' : (isAndroid ? 'android' : 'desktop');

        const selectTab = (name) => {
          tabs.forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
          panels.forEach((p) => { p.style.display = p.dataset.panel === name ? '' : 'none'; });
        };
        tabs.forEach((t) => t.addEventListener('click', () => selectTab(t.dataset.tab)));
        selectTab(detected);

        // Native install button (Android / desktop Chromium).
        const cta = document.getElementById('install-cta');
        const installNow = document.getElementById('install-now');
        const sync = () => { if (window._pwaPrompt && cta) cta.style.display = ''; };
        document.addEventListener('pwa-installable', sync);
        sync();
        if (installNow) installNow.addEventListener('click', () => {
          if (!window._pwaPrompt) return;
          window._pwaPrompt.prompt();
          window._pwaPrompt.userChoice.finally(() => { window._pwaPrompt = null; if (cta) cta.style.display = 'none'; });
        });
      }
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
