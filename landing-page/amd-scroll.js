/* ZENITH — pinned scroll-driven AMD strategy chart.
   The #how section pins; scroll progress 0→1 builds a candlestick trade
   through the four phases (Accumulation → Manipulation → Distribution →
   Execution) with annotations fading in on cue. */
(function () {
  'use strict';
  var C = { green: '#00C896', greenL: 'rgba(0,200,150,.5)', mint: '#2FEFC0',
            red: '#FF5C5C', amber: '#F5A623', grid: 'rgba(139,148,158,.07)' };

  // ---- price domain & landmarks ----
  var N = 64, PMIN = 86, PMAX = 128;
  var rangeLo = 100, rangeHi = 108, rangeFrom = 1, rangeTo = 26;
  var sweepIdx = 28, sweepLo = 90;
  var fvgFrom = 28, fvgTo = 30, fvgLo = 99.5, fvgHi = 103;
  var entry = 101, entryIdx = 30, sl = 90, tp = 123;   // SL == sweep wick low

  // ---- price path: keyframes → interpolated closes, then candles ----
  var WP = [
    [0, 104], [3, 107.2], [7, 100.8], [11, 106.6], [15, 100.4], [19, 107.4], [23, 100.8], [26, 103.5],
    [27, 99.5], [28, 99], [29, 102.6], [30, 101.2],          // sweep + FVG reversal + entry
    [34, 110], [38, 113], [41, 106.8], [45, 116.2], [49, 108], [52, 110.5],
    [55, 106.8], [58, 109], [61, 117], [63, 123]
  ];
  function rnd(i) { var x = Math.sin(i * 127.1 + 0.3) * 43758.5453; return x - Math.floor(x); }
  function interp() {
    var out = [];
    for (var i = 0; i < N; i++) {
      var a = WP[0], b = WP[WP.length - 1];
      for (var k = 1; k < WP.length; k++) { if (i <= WP[k][0]) { a = WP[k - 1]; b = WP[k]; break; } }
      var t = (i - a[0]) / (b[0] - a[0]);
      var base = a[1] + (b[1] - a[1]) * t;
      out.push(base + (rnd(i * 1.7) - 0.5) * (i <= rangeTo ? 2.3 : 2.7));
    }
    return out;
  }
  function buildBars() {
    var cl = interp(), bars = [], prev = cl[0] - 1.2;
    for (var i = 0; i < N; i++) {
      var o = prev, c = cl[i];
      var hi = Math.max(o, c) + (0.4 + rnd(i + 2) * 1.7);
      var lo = Math.min(o, c) - (0.4 + rnd(i + 5) * 1.7);
      if (i <= rangeTo) { hi = Math.min(hi, rangeHi + 0.9); lo = Math.max(lo, rangeLo - 0.7); }
      if (i === 27) { o = 103.4; c = 99.4; hi = 103.8; lo = 98.8; }        // impulse down
      if (i === sweepIdx) { o = 99.2; c = 99.7; lo = sweepLo; hi = 100.2; } // the sweep wick (== SL)
      if (i === 29) { o = 99.9; c = 102.7; hi = 103.3; lo = 99.5; }        // strong FVG candle
      bars.push({ o: o, h: hi, l: lo, c: c }); prev = c;
    }
    return bars;
  }
  var BARS = buildBars();

  // ---- helpers ----
  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function smooth(e0, e1, x) { var t = clamp((x - e0) / (e1 - e0), 0, 1); return t * t * (3 - 2 * t); }
  function lerpStops(stops, p) {
    for (var i = 1; i < stops.length; i++) {
      if (p <= stops[i][0]) { var a = stops[i - 1], b = stops[i], t = (p - a[0]) / (b[0] - a[0]); return a[1] + (b[1] - a[1]) * t; }
    }
    return stops[stops.length - 1][1];
  }
  var VIS = [[0, 6], [0.27, 27], [0.44, 31], [0.58, 34], [1, N]];

  // ---- canvas ----
  var canvas = document.getElementById('amd-chart');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var W = 0, H = 0, padL = 14, padR = 60, padT = 20, padB = 24;

  function resize() {
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function X(i) { return padL + (i + 0.5) / N * (W - padL - padR); }
  function Y(p) { return padT + (1 - (p - PMIN) / (PMAX - PMIN)) * (H - padT - padB); }
  function chartR() { return W - padR; }

  function tag(x, y, text, color, align) {
    ctx.font = '600 11px JetBrains Mono, monospace';
    var w = ctx.measureText(text).width + 12;
    var tx = align === 'right' ? x - w : x;
    ctx.fillStyle = 'rgba(11,13,18,.9)'; roundRect(tx, y - 9, w, 18, 4); ctx.fill();
    ctx.fillStyle = color; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    ctx.fillText(text, tx + 6, y + 0.5);
  }
  function roundRect(x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  function dashLine(x1, y1, x2, y2, color, alpha, dash) {
    ctx.save(); ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = 1;
    ctx.setLineDash(dash || [4, 4]); ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.restore();
  }

  function draw(p) {
    if (!W) resize();
    ctx.clearRect(0, 0, W, H);

    // gridlines
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
    for (var g = 90; g <= 125; g += 5) { ctx.beginPath(); ctx.moveTo(padL, Y(g)); ctx.lineTo(chartR(), Y(g)); ctx.stroke(); }

    var vis = Math.round(lerpStops(VIS, p));
    var cw = Math.max(3.5, (W - padL - padR) / N * 0.62);

    // ---- accumulation range box (behind) ----
    var aR = smooth(0.04, 0.13, p);
    if (aR > 0.01) {
      ctx.save(); ctx.globalAlpha = aR;
      ctx.fillStyle = 'rgba(174,180,189,.05)';
      var rx = X(rangeFrom) - cw, rw = X(rangeTo) - X(rangeFrom) + cw * 2;
      ctx.fillRect(rx, Y(rangeHi), rw, Y(rangeLo) - Y(rangeHi));
      ctx.strokeStyle = 'rgba(174,180,189,.3)'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
      ctx.strokeRect(rx, Y(rangeHi), rw, Y(rangeLo) - Y(rangeHi)); ctx.setLineDash([]); ctx.restore();
    }

    // ---- execution zones (behind candles): green TP zone + red SL zone ----
    var zA = smooth(0.6, 0.72, p);
    if (zA > 0.01) {
      var zx = X(entryIdx) - cw, zw = chartR() - zx;
      ctx.save(); ctx.globalAlpha = zA;
      ctx.fillStyle = 'rgba(0,200,150,.10)'; ctx.fillRect(zx, Y(tp), zw, Y(entry) - Y(tp));
      ctx.fillStyle = 'rgba(255,92,92,.10)'; ctx.fillRect(zx, Y(entry), zw, Y(sl) - Y(entry));
      ctx.restore();
    }

    // ---- candles ----
    for (var i = 0; i < vis; i++) {
      var b = BARS[i], up = b.c >= b.o, col = up ? C.green : C.red, x = X(i);
      ctx.strokeStyle = col; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, Y(b.h)); ctx.lineTo(x, Y(b.l)); ctx.stroke();
      var yo = Y(b.o), yc = Y(b.c);
      ctx.fillStyle = col; ctx.fillRect(x - cw / 2, Math.min(yo, yc), cw, Math.max(1.5, Math.abs(yo - yc)));
    }

    // ---- manipulation: liquidity line + sweep flag ----
    var mA = smooth(0.28, 0.4, p) * (1 - smooth(0.62, 0.74, p));
    if (mA > 0.01 && vis > sweepIdx) {
      dashLine(padL, Y(rangeLo), chartR(), Y(rangeLo), C.red, mA * 0.85, [5, 4]);
      ctx.save(); ctx.globalAlpha = mA; tag(X(sweepIdx) + 10, Y(sweepLo), 'Liquidity swept', C.red, 'left'); ctx.restore();
    }

    // ---- distribution: FVG box + entry arrow ----
    var fA = smooth(0.46, 0.56, p);
    if (fA > 0.01 && vis > fvgTo) {
      ctx.save(); ctx.globalAlpha = fA;
      ctx.fillStyle = 'rgba(245,166,35,.18)';
      ctx.fillRect(X(fvgFrom) - cw / 2, Y(fvgHi), X(fvgTo) - X(fvgFrom) + cw, Y(fvgLo) - Y(fvgHi));
      ctx.strokeStyle = 'rgba(245,166,35,.6)'; ctx.lineWidth = 1; ctx.strokeRect(X(fvgFrom) - cw / 2, Y(fvgHi), X(fvgTo) - X(fvgFrom) + cw, Y(fvgLo) - Y(fvgHi));
      tag(X(fvgFrom) - cw / 2, Y(fvgHi) - 11, 'FVG', C.amber, 'left'); ctx.restore();
    }
    var eA = smooth(0.5, 0.6, p);
    if (eA > 0.01 && vis > entryIdx) {
      // long entry marker
      ctx.save(); ctx.globalAlpha = eA;
      ctx.fillStyle = C.green; var ex = X(entryIdx), ey = Y(entry) + 16;
      ctx.beginPath(); ctx.moveTo(ex, ey - 9); ctx.lineTo(ex - 4.5, ey); ctx.lineTo(ex + 4.5, ey); ctx.closePath(); ctx.fill();
      ctx.restore();
      dashLine(X(entryIdx) - cw, Y(entry), chartR(), Y(entry), C.green, eA, [2, 3]);
      ctx.save(); ctx.globalAlpha = eA; tag(W - 6, Y(entry), 'Entry · Long', C.green, 'right'); ctx.restore();
    }

    // ---- execution: SL / TP lines + tags + R readout ----
    if (zA > 0.01) {
      dashLine(X(entryIdx) - cw, Y(sl), chartR(), Y(sl), C.red, zA, [5, 4]);
      dashLine(X(entryIdx) - cw, Y(tp), chartR(), Y(tp), C.green, zA, [5, 4]);
      ctx.save(); ctx.globalAlpha = zA;
      tag(W - 6, Y(sl), 'SL · −1R', C.red, 'right');
      tag(W - 6, Y(tp), 'TP · +2R', C.green, 'right'); ctx.restore();
    }
  }

  // ---- step copy + scroll wiring ----
  var steps = Array.prototype.slice.call(document.querySelectorAll('.amd-step'));
  var fills = Array.prototype.slice.call(document.querySelectorAll('.amd-step .amd-bar > span'));
  var bounds = [[0, 0.27], [0.27, 0.46], [0.46, 0.6], [0.6, 1.0001]];
  var readout = document.getElementById('amd-readout');
  var RLABEL = ['Price coils into a range', 'A wick sweeps the lows', 'FVG confirms — entry fires', 'Strong move hits +2R'];

  function activeIndex(p) {
    for (var i = 0; i < bounds.length; i++) if (p >= bounds[i][0] && p < bounds[i][1]) return i;
    return p < 0 ? 0 : 3;
  }
  function render(p) {
    draw(p);
    var ai = activeIndex(p);
    var n = steps.length;
    for (var i = 0; i < steps.length; i++) {
      var depth = (i - ai + n) % n;            // 0 = front, 1..3 = stacked behind (mobile deck)
      steps[i].style.setProperty('--depth', depth);
      steps[i].style.zIndex = String(n - depth);
      steps[i].classList.toggle('on', i === ai);
      steps[i].classList.toggle('done', i < ai);
      if (fills[i]) {
        var local = clamp((p - bounds[i][0]) / (bounds[i][1] - bounds[i][0]), 0, 1);
        fills[i].style.transform = 'scaleX(' + (i < ai ? 1 : i === ai ? local : 0) + ')';
      }
    }
    if (readout) {
      readout.innerHTML = '<span class="r-phase">' + ('0' + (ai + 1)) + ' · ' + RLABEL[ai] + '</span>';
    }
  }

  var track = document.getElementById('amd-track');
  var ticking = false, lastP = 0;
  function onScroll() {
    if (!track) return;
    var top = track.getBoundingClientRect().top;
    var total = track.offsetHeight - window.innerHeight;
    lastP = clamp(-top / total, 0, 1);
    if (!ticking) { ticking = true; requestAnimationFrame(function () { render(lastP); ticking = false; }); }
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', function () { resize(); render(lastP); });
  window.__amdRender = function (p) { render(clamp(p, 0, 1)); };
  resize(); onScroll(); render(0);
})();
