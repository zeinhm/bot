/* ZENITH — scroll-driven equity-curve replay for the Track Record section.
   The #performance section pins; scroll progress 0→1 replays all 886 real
   trades (from trade_log.csv). The equity curve draws trade-by-trade while
   the axes AUTO-FIT to the data shown so far (Y levels grow $10k→…→$383k on
   the LEFT, X dates grow 2020→…→2026), and the stat panel + chart % count up
   with the running win rate. BI-DIRECTIONAL: scroll up rewinds the replay so
   you can land on any exact trade/moment. */
(function () {
  'use strict';
  var D = window.ZBT;
  var canvas = document.getElementById('bt-chart');
  if (!D || !canvas) return;
  var ctx = canvas.getContext('2d');
  var GREEN = '#00C896', AREA0 = 'rgba(0,200,150,.20)', AREA1 = 'rgba(0,200,150,0)';
  var AXIS = '#5C636E', AXISMAJ = '#AEB4BD', GRID = 'rgba(139,148,158,.10)';

  var W = 0, H = 0, padL = 46, padR = 12, padT = 14, padB = 24;
  function resize() {
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  var MS_DAY = 86400000;
  var BASE = new Date(D.baseDate + 'T00:00:00');
  function fmtAxis(v) {
    if (v >= 1000) return '$' + ((v / 1000) % 1 === 0 ? (v / 1000) : (v / 1000).toFixed(1)) + 'k';
    return '$' + Math.round(v);
  }
  function niceCeil(x) {
    if (x <= 0) return 1;
    var exp = Math.floor(Math.log10(x)), f = x / Math.pow(10, exp);
    var nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
    return nf * Math.pow(10, exp);
  }
  function yTicks(max) {
    var step = niceCeil(max / 4), ticks = [];
    for (var v = 0; v <= max + 1e-6; v += step) ticks.push(v);
    return ticks;
  }
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function xTicks(maxDay) {
    var step = maxDay > 1095 ? 12 : maxDay > 548 ? 6 : maxDay > 270 ? 3 : 2;  // months
    var ticks = [];
    var d = new Date(BASE.getFullYear(), 0, 1);   // start at January of base year
    while (true) {
      var day = Math.round((d - BASE) / MS_DAY);
      if (day > maxDay + 1) break;
      if (day >= -10) {
        var major = d.getMonth() === 0;
        ticks.push({ day: clamp(day, 0, maxDay), label: major ? String(d.getFullYear()) : MON[d.getMonth()], major: major });
      }
      d = new Date(d.getFullYear(), d.getMonth() + step, 1);
    }
    return ticks;
  }

  function draw(p) {
    if (!W) resize();
    ctx.clearRect(0, 0, W, H);
    var n = D.n, k = Math.round(p * n);
    var maxDay = 20, maxEq = D.start;
    for (var i = 0; i < k; i++) {
      if (D.dayoff[i] > maxDay) maxDay = D.dayoff[i];   // running max → monotonic X
      if (D.equity[i] > maxEq) maxEq = D.equity[i];
    }
    var yMax = niceCeil(maxEq * 1.05);

    var plotR = W - padR, plotB = H - padB, plotL = padL;
    function X(idx) { return plotL + (idx / Math.max(k, 1)) * (plotR - plotL); }
    function Y(v) { return padT + (1 - v / yMax) * (plotB - padT); }
    function dayToIdx(day) { for (var q = 0; q < k; q++) if (D.dayoff[q] >= day) return q + 1; return k; }

    // gridlines + LEFT y-axis labels
    var yt = yTicks(yMax);
    ctx.font = '500 10.5px JetBrains Mono, monospace';
    ctx.textBaseline = 'middle';
    for (var t = 0; t < yt.length; t++) {
      var yy = Y(yt[t]);
      ctx.strokeStyle = GRID; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(plotL, yy); ctx.lineTo(plotR, yy); ctx.stroke();
      ctx.fillStyle = AXIS; ctx.textAlign = 'right';
      ctx.fillText(fmtAxis(yt[t]), plotL - 8, yy);
    }
    // x-axis labels along the bottom
    var xt = xTicks(maxDay);
    ctx.textBaseline = 'alphabetic';
    for (var j = 0; j < xt.length; j++) {
      var xx = X(dayToIdx(xt[j].day));
      if (xx < plotL - 1 || xx > plotR + 1) continue;
      ctx.fillStyle = xt[j].major ? AXISMAJ : AXIS;
      ctx.font = (xt[j].major ? '600 ' : '500 ') + '10px JetBrains Mono, monospace';
      ctx.textAlign = 'center';
      ctx.fillText(xt[j].label, clamp(xx, plotL + 8, plotR - 8), H - 7);
    }

    if (k < 1) return;

    // build path: synthetic start, then one point per trade spaced EVENLY by
    // trade sequence (not calendar date) — so clusters of same-day trades don't
    // stack into vertical risers, and the curve reads smooth and compact.
    var pts = [[0, D.start]];
    for (var m = 0; m < k; m++) pts.push([m + 1, D.equity[m]]);
    var lastX = X(pts[pts.length - 1][0]), lastY = Y(pts[pts.length - 1][1]);

    // smooth path tracer (midpoint quadratics) — used for both line and area
    function trace() {
      ctx.moveTo(X(pts[0][0]), Y(pts[0][1]));
      for (var i = 1; i < pts.length - 1; i++) {
        var x0 = X(pts[i][0]), y0 = Y(pts[i][1]);
        var x1 = X(pts[i + 1][0]), y1 = Y(pts[i + 1][1]);
        ctx.quadraticCurveTo(x0, y0, (x0 + x1) / 2, (y0 + y1) / 2);
      }
      ctx.lineTo(lastX, lastY);
    }

    // area fill
    ctx.beginPath();
    trace();
    ctx.lineTo(lastX, plotB); ctx.lineTo(X(pts[0][0]), plotB); ctx.closePath();
    var grad = ctx.createLinearGradient(0, padT, 0, plotB);
    grad.addColorStop(0, AREA0); grad.addColorStop(1, AREA1);
    ctx.fillStyle = grad; ctx.fill();

    // line
    ctx.beginPath();
    trace();
    ctx.strokeStyle = GREEN; ctx.lineWidth = 1.7; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.shadowColor = 'rgba(0,200,150,.4)'; ctx.shadowBlur = 4;
    ctx.stroke(); ctx.shadowBlur = 0;

    // leading dot
    ctx.fillStyle = GREEN; ctx.beginPath(); ctx.arc(lastX, lastY, 2.6, 0, 7); ctx.fill();
  }

  // ---- DOM stat updates ----
  var elBig = document.getElementById('bt-big');
  var elTrades = document.getElementById('bt-trades');
  var elWR = document.getElementById('bt-wr');
  var elPct = document.getElementById('bt-pct');
  var elHint = document.getElementById('bt-hint');
  function commas(v) { return Math.round(v).toLocaleString('en-US'); }
  // exact-ish: one decimal under $20k (so a drawdown reads "$9.7k"), whole k above
  function bigFmt(v) { return v < 20000 ? '$' + (Math.floor(v / 100) / 10).toFixed(1) + 'k' : '$' + Math.round(v / 1000) + 'k'; }
  function pctFmt(a) { return a < 100 ? a.toFixed(1) : commas(a); }
  function paint(gain) {
    var col = gain < 0 ? 'var(--red)' : 'var(--green)';
    if (elBig) elBig.style.color = col;
    if (elPct) elPct.style.color = col;
  }
  function stats(p) {
    var n = D.n, k = Math.round(p * n);
    if (k < 1) {
      if (elBig) elBig.textContent = '$10k';
      if (elTrades) elTrades.textContent = '0';
      if (elWR) elWR.textContent = '0%';
      if (elPct) elPct.textContent = '+$0 (+0.0%)';
      paint(0);
      if (elHint) elHint.style.opacity = '';
      return;
    }
    var i = k - 1, eq = D.equity[i], wins = D.cumWins[i];
    var wr = wins / k * 100, pct = (eq / D.start - 1) * 100;
    var gain = eq - D.start, sgn = gain < 0 ? '\u2212' : '+';   // − = true minus sign
    if (elBig) elBig.textContent = bigFmt(eq);
    if (elTrades) elTrades.textContent = commas(k);
    if (elWR) elWR.textContent = wr.toFixed(1) + '%';
    if (elPct) elPct.textContent = sgn + '$' + commas(Math.abs(gain)) + ' (' + sgn + pctFmt(Math.abs(pct)) + '%)';
    paint(gain);
    if (elHint) elHint.style.opacity = (p > 0.04 && p < 0.99) ? '' : '0';
  }

  function render(p) { draw(p); stats(p); }

  // ---- scroll wiring (bi-directional — maps straight to scroll, both ways) ----
  var track = document.getElementById('bt-track');
  var stick = track ? track.querySelector('.perf-stick') : null;
  var ticking = false;
  function pinned() { return stick && getComputedStyle(stick).position === 'sticky'; }
  function onScroll() {
    if (!track) return;
    if (!pinned()) { render(1); return; }     // mobile / reduced-motion: show complete
    var top = track.getBoundingClientRect().top;
    var total = track.offsetHeight - window.innerHeight;
    var p = total > 0 ? clamp(-top / total, 0, 1) : 1;
    if (!ticking) { ticking = true; requestAnimationFrame(function () { render(p); ticking = false; }); }
  }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', function () { resize(); onScroll(); });
  window.__btRender = function (p) { render(clamp(p, 0, 1)); };  // debug hook
  resize();
  if (pinned()) render(0); else render(1);
  onScroll();
})();
