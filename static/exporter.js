// Canvas-based image exporter. Builds a poster-style PNG from scratch
// (no DOM scraping, no html2canvas) so output is consistent across
// browsers/OSes and renders correctly even when Chart.js canvases
// would otherwise come out blank.
//
// Two modes: "highlights" (KPIs + savings + daily activity) and "full"
// (adds heatmap, models, where the spend went, top projects, top conversations).
//
// The palette is read from the page's CSS custom properties at export time,
// so the PNG follows the theme the viewer chose (light or dark). Data comes
// through the app's api() helper, so the active window, account and project
// filters apply exactly as on screen.

(() => {
  const W = 1200;
  const PAD = 28;
  let C = {};

  function readPalette() {
    const cs = getComputedStyle(document.documentElement);
    const v = n => cs.getPropertyValue(n).trim();
    C = {
      dark: cs.getPropertyValue("color-scheme").trim() === "dark",
      bg: v("--bg"), panel: v("--panel"), panel2: v("--panel-2"), panel3: v("--panel-3"),
      border: v("--border"), borderSoft: v("--border-soft"),
      text: v("--text"), text2: v("--text-2"), muted: v("--muted"),
      accent: v("--accent"), accentText: v("--accent-text"),
      good: v("--good"), goodText: v("--good-text"),
      track: v("--track"), hatchInk: v("--hatch-ink"),
      chart: [1,2,3,4,5].map(i => v(`--chart-${i}`)), other: v("--chart-other"),
      heat: [0,1,2,3,4,5].map(i => v(`--heat-${i}`)),
    };
  }

  const SANS = '"Instrument Sans", -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
  const SERIF = '"Source Serif 4", Georgia, "Times New Roman", serif';

  // Official Claude mark, drawn as Path2D so it scales without rasterization.
  const CLAUDE_MARK_PATH = "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z";
  const CLAUDE_MARK = (typeof Path2D !== "undefined") ? new Path2D(CLAUDE_MARK_PATH) : null;

  function drawClaudeMark(ctx, cx, cy, size, color) {
    ctx.save();
    const s = size / 24;
    ctx.translate(cx - size / 2, cy - size / 2);
    ctx.scale(s, s);
    ctx.fillStyle = color;
    if (CLAUDE_MARK) ctx.fill(CLAUDE_MARK);
    ctx.restore();
  }

  // ---------- formatting helpers (mirror app.js) ----------
  const fmtInt = n => (n == null) ? "—" : Math.round(n).toLocaleString();
  const fmtCompact = n => (n == null) ? "—" : Intl.NumberFormat("en", {notation:"compact", maximumFractionDigits:1}).format(n);
  const fmtMoney = n => (n == null) ? "—" : "$" + (Math.round(n*100)/100).toLocaleString("en", {minimumFractionDigits:2, maximumFractionDigits:2});
  // Dates come from app.js (Intl, browser locale): "Sep 24", "Feb 7 – Oct 7, 2026".
  const fmtDay = s => (window.__dash?.fmtDay || (x => x || "—"))(s);
  const fmtRange = (a, b) => (window.__dash?.fmtRange || ((x, y) => `${x} – ${y}`))(a, b);
  const shortModel = id => (window.__dash?.shortModel || (x => x))(id);

  // ---------- canvas utilities ----------
  function makeCanvas(w, h, dpr = 2) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.textBaseline = "alphabetic";
    return { canvas, ctx };
  }
  function rrect(ctx, x, y, w, h, r) {
    const rr = Math.max(0, Math.min(r, w/2, h/2));
    ctx.beginPath();
    ctx.moveTo(x+rr, y);
    ctx.arcTo(x+w, y, x+w, y+h, rr);
    ctx.arcTo(x+w, y+h, x, y+h, rr);
    ctx.arcTo(x, y+h, x, y, rr);
    ctx.arcTo(x, y, x+w, y, rr);
    ctx.closePath();
  }
  function fill(ctx, x, y, w, h, r, color) { ctx.fillStyle = color; rrect(ctx, x, y, w, h, r); ctx.fill(); }
  function stroke(ctx, x, y, w, h, r, color, lw = 1) { ctx.strokeStyle = color; ctx.lineWidth = lw; rrect(ctx, x + .5, y + .5, w - 1, h - 1, r); ctx.stroke(); }
  function card(ctx, x, y, w, h) { fill(ctx, x, y, w, h, 12, C.panel); stroke(ctx, x, y, w, h, 12, C.borderSoft); }
  function text(ctx, str, x, y, opts = {}) {
    const { size = 13, weight = 400, color = C.text, family = SANS, align = "left", baseline = "alphabetic" } = opts;
    ctx.font = `${weight} ${size}px ${family}`;
    ctx.fillStyle = color; ctx.textAlign = align; ctx.textBaseline = baseline;
    ctx.fillText(str, x, y);
  }
  function label(ctx, str, x, y, color = C.muted) {
    ctx.save();
    ctx.font = `600 10px ${SANS}`;
    if ("letterSpacing" in ctx) ctx.letterSpacing = "0.9px";
    ctx.fillStyle = color; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
    ctx.fillText(str.toUpperCase(), x, y);
    ctx.restore();
  }
  function ellipsize(ctx, str, maxW, opts = {}) {
    const { size = 13, weight = 400, family = SANS } = opts;
    ctx.font = `${weight} ${size}px ${family}`;
    str = String(str ?? "");
    if (ctx.measureText(str).width <= maxW) return str;
    let s = str;
    while (s.length > 2 && ctx.measureText(s + "…").width > maxW) s = s.slice(0, -1);
    return s + "…";
  }
  function alpha(hex, a) {
    if (!hex || !hex.startsWith("#")) return hex;
    const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
    return `rgba(${r},${g},${b},${a})`;
  }
  function heatColor(v, max) {
    if (!v || max <= 0) return C.heat[0];
    const t = Math.log10(1 + v) / Math.log10(1 + max);
    return C.heat[Math.min(5, Math.max(1, Math.ceil(t * 5)))];
  }
  function hatch(ctx, base) {
    const s = 8, c = document.createElement("canvas");
    c.width = s; c.height = s;
    const x = c.getContext("2d");
    x.fillStyle = base; x.fillRect(0, 0, s, s);
    x.strokeStyle = C.hatchInk; x.lineWidth = 1.6;
    x.beginPath(); x.moveTo(-1, s + 1); x.lineTo(s + 1, -1); x.moveTo(-1, 1); x.lineTo(1, -1); x.moveTo(s - 1, s + 1); x.lineTo(s + 1, s - 1); x.stroke();
    return ctx.createPattern(c, "repeat");
  }

  // ---------- section drawers ----------
  function drawHeader(ctx, y, data, mode) {
    const x = PAD, w = W - PAD * 2, h = 76;
    card(ctx, x, y, w, h);
    const markCx = x + 38, markCy = y + h / 2, markSize = 32;
    drawClaudeMark(ctx, markCx, markCy, markSize, C.accent);
    const titleX = markCx + markSize / 2 + 14;
    text(ctx, "Claude Usage", titleX, y + 34, { size: 23, weight: 600, family: SERIF });
    const f = data.filters;
    const range = f.range || "no data";
    const chips = f.list.filter(x => x.key !== "day").map(x => `${x.kind.toLowerCase()}: ${x.label}`);
    const view = f.day ? "one day" : f.windowName;
    text(ctx, ellipsize(ctx, [range, view, ...chips].join("  ·  "), w - (titleX - x) - 300, { size: 12 }), titleX, y + 55, { size: 12, color: C.muted });
    text(ctx, mode === "full" ? "Full report" : "Highlights", x + w - 18, y + 30, { size: 13, weight: 600, color: C.text2, align: "right" });
    text(ctx, "runs locally · no data leaves your machine", x + w - 18, y + 49, { size: 11, color: C.muted, align: "right" });
    text(ctx, `generated ${new Date().toLocaleString()}`, x + w - 18, y + 64, { size: 10.5, color: C.muted, align: "right" });
    return h;
  }

  function drawKPIs(ctx, y, data) {
    const x = PAD, w = W - PAD * 2;
    const s = data.summary;
    const cards = [
      { k: "Would have cost", v: fmtMoney(s.api_cost), sub: "at standard API rates", hero: true },
      { k: "Total tokens", v: fmtCompact(s.total_tokens), sub: `in ${fmtCompact(s.input_tokens)} · out ${fmtCompact(s.output_tokens)}${s.thinking_tokens > 0 ? ` (${fmtCompact(s.thinking_tokens)} thinking)` : ""} · cache ${fmtCompact(s.cache_5m_write + s.cache_1h_write + s.cache_read)}` },
      { k: "Messages", v: fmtInt(s.msgs), sub: `${fmtInt(s.sessions)} sessions · ${fmtInt(s.projects)} projects` },
    ];
    if (s.subscription_cost > 0) {
      // Out of pocket; an employer-paid plan is named separately (older servers: all of it is "you").
      const emp = s.paid_by_employer_usd || 0, you = s.paid_by_you_usd ?? Math.max(0, s.subscription_cost - emp);
      const empSet = new Set(s.employer_accounts || []);
      const plans = f => (s.billing_charges || []).filter(f).map(c => c.plan).filter((v, i, a) => a.indexOf(v) === i).join(" · ");
      const sub = [you > 0 || !emp ? plans(c => !empSet.has(c.account)) : "", emp > 0 ? `excl. ${fmtMoney(emp)} ${plans(c => empSet.has(c.account)) || "plan"} (employer)` : ""].filter(Boolean).join(" · ");
      cards.push({ k: "You paid", v: fmtMoney(you), sub });
    }
    if ((s.fast_cost_usd || 0) > 0) cards.push({ k: "Fast-mode premium", v: fmtMoney(s.fast_premium_usd), sub: `${(s.fast_cost_usd / (s.api_cost || 1) * 100).toFixed(1)}% of spend in fast mode` });
    const n = cards.length, gap = 12, ch = 104;
    const cw = (w - gap * (n - 1)) / n;
    cards.forEach((c, i) => {
      const cx = x + i * (cw + gap);
      if (c.hero) { fill(ctx, cx, y, cw, ch, 12, C.panel2); stroke(ctx, cx, y, cw, ch, 12, C.borderSoft); }
      else card(ctx, cx, y, cw, ch);
      label(ctx, c.k, cx + 16, y + 24, c.hero ? C.accentText : C.muted);
      text(ctx, ellipsize(ctx, c.v, cw - 32, { size: 27, weight: 600, family: SERIF }), cx + 16, y + 60, { size: 27, weight: 600, family: SERIF, color: c.hero ? C.accentText : C.text });
      if (c.sub) text(ctx, ellipsize(ctx, c.sub, cw - 32, { size: 11 }), cx + 16, y + 84, { size: 11, color: C.muted });
    });
    return ch;
  }

  function drawSavingsHero(ctx, y, data) {
    const s = data.summary;
    if (!(s.subscription_cost > 0)) return 0;
    const x = PAD, w = W - PAD * 2, h = 132;
    fill(ctx, x, y, w, h, 14, C.panel);
    fill(ctx, x, y, w, h, 14, alpha(C.good, C.dark ? 0.08 : 0.06));
    stroke(ctx, x, y, w, h, 14, alpha(C.good, 0.35));
    label(ctx, "Saved by subscribing", x + 22, y + 30, C.goodText);
    const emp = s.paid_by_employer_usd || 0, you = s.paid_by_you_usd ?? Math.max(0, s.subscription_cost - emp);
    const sav = s.savings_you ?? (emp ? s.api_cost - you : s.savings);
    const selfApi = emp ? sav + you : s.api_cost;   // savings_you covers only the accounts you pay for
    const mult = emp ? (you > 0 ? Math.round(selfApi / you * 10) / 10 : null) : s.multiplier;
    text(ctx, (sav >= 0 ? "+" : "−") + fmtMoney(Math.abs(sav)), x + 22, y + 82, { size: 50, weight: 600, family: SERIF, color: C.goodText });
    // Walks from "Would have cost" to the saving, as the dashboard card does.
    const empApi = Math.max(s.api_cost - selfApi, 0);
    const acctNames = typeof acctName === "function" ? acctName : (a => a);
    const empNames = (s.employer_accounts || []).map(acctNames).join(", ") || "employer accounts";
    text(ctx, emp ? `${fmtMoney(s.api_cost)} at API rates − ${fmtMoney(empApi)} on ${empNames} (employer-paid) − ${fmtMoney(you)} you paid`
                  : `${fmtMoney(s.api_cost)} at API rates − ${fmtMoney(s.subscription_cost)} you paid`, x + 22, y + 110, { size: 13, color: C.text2 });
    if (mult) {
      const badge = `${mult}× cheaper than API`;
      ctx.font = `600 12.5px ${SANS}`;
      const bw = ctx.measureText(badge).width + 24, bh = 30;
      const bx = x + w - bw - 22, by = y + 22;
      fill(ctx, bx, by, bw, bh, 999, alpha(C.good, 0.14));
      stroke(ctx, bx, by, bw, bh, 999, alpha(C.good, 0.4));
      text(ctx, badge, bx + bw / 2, by + 20, { size: 12.5, weight: 600, color: C.goodText, align: "center" });
    }
    return h;
  }

  // Daily API-rate cost bars (fast-mode share hatched) with the cumulative line,
  // and the day's tokens as a muted strip beneath.
  function drawDaily(ctx, y, data) {
    const x = PAD, w = W - PAD * 2, h = 250;
    card(ctx, x, y, w, h);
    const hourly = !!data.filters.hourly;
    label(ctx, hourly ? "Hourly activity · API-rate cost" : "Daily activity · API-rate cost", x + 18, y + 26);
    let rows = data.timeseries?.rows || [];
    // An hourly view (today, a drilled-in day, a short custom range) draws every hour, so quiet
    // hours read as zero (as on the page).
    if (hourly && rows.length) {
      const by = new Map(rows.map(r => [r.bucket, r]));
      rows = window.__dash.hourBuckets().map(b => by.get(b) || { bucket: b });
    }
    const multiDay = hourly && rows.length > 24;
    const tick = b => hourly ? `${multiDay ? fmtDay(String(b).slice(0, 10)) + " " : ""}${String(b).slice(11, 13)}:00` : fmtDay(b);
    if (!rows.length) { text(ctx, "No activity in window.", x + 18, y + 64, { size: 12, color: C.muted }); return h; }
    const hasFast = rows.some(r => (r.fast_cost_usd || 0) > 0);

    // legend, right-aligned
    const items = [["swatch", C.chart[0], hourly ? "cost / hour" : "cost / day"]];
    if (hasFast) items.push(["hatch", null, "fast mode"]);
    items.push(["line", C.text2, "cumulative"]);
    let lx = x + w - 18;
    for (const [kind, col, lab] of items.reverse()) {
      ctx.font = `500 11px ${SANS}`;
      const tw = ctx.measureText(lab).width;
      text(ctx, lab, lx, y + 26, { size: 11, color: C.text2, align: "right" });
      lx -= tw + 8;
      if (kind === "line") { ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(lx - 16, y + 22); ctx.lineTo(lx, y + 22); ctx.stroke(); lx -= 16; }
      else { ctx.fillStyle = kind === "hatch" ? hatch(ctx, alpha(C.accent, .28)) : col; rrect(ctx, lx - 11, y + 17, 11, 10, 2); ctx.fill(); lx -= 11; }
      lx -= 16;
    }

    const px = x + 64, py = y + 48, pw = w - 64 - 70, ph = h - 48 - 74;
    // round axis maxima up to a clean step so ticks read 0 / 250 / 500 ...
    const nice = v => { const p = Math.pow(10, Math.floor(Math.log10(v / 4))); const s = [1, 2, 2.5, 5, 10].find(m => m * p * 4 >= v) * p; return s * 4; };
    const maxCost = nice(Math.max(...rows.map(r => r.cost || 0), 0.01));
    let cum = 0;
    const cumPts = rows.map(r => (cum += r.cost || 0));
    const maxCum = nice(Math.max(cum, 0.01));
    // gridlines + left (per day) and right (cumulative) ticks
    for (let i = 0; i <= 4; i++) {
      const gy = py + ph - (i / 4) * ph;
      ctx.strokeStyle = C.borderSoft; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px, Math.round(gy) + .5); ctx.lineTo(px + pw, Math.round(gy) + .5); ctx.stroke();
      text(ctx, "$" + fmtCompact(maxCost * i / 4), px - 8, gy + 4, { size: 10, color: C.muted, align: "right" });
      text(ctx, "$" + fmtCompact(maxCum * i / 4), px + pw + 8, gy + 4, { size: 10, color: C.muted });
    }
    const bw = pw / rows.length;
    const barW = Math.max(1, Math.min(24, bw - Math.min(3, bw * 0.25)));
    const fastPat = hatch(ctx, alpha(C.accent, .28));
    rows.forEach((r, i) => {
      const bx = px + i * bw + (bw - barW) / 2;
      const total = r.cost || 0, fast = r.fast_cost_usd || 0;
      const hStd = ((total - fast) / maxCost) * ph, hFast = (fast / maxCost) * ph;
      ctx.fillStyle = C.chart[0];
      ctx.fillRect(bx, py + ph - hStd, barW, hStd);
      if (hFast > 0) { ctx.fillStyle = fastPat; ctx.fillRect(bx, py + ph - hStd - hFast, barW, hFast); }
    });
    ctx.strokeStyle = C.text2; ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    rows.forEach((_, i) => {
      const cx = px + i * bw + bw / 2, cy = py + ph - (cumPts[i] / maxCum) * ph;
      if (i === 0) ctx.moveTo(cx, cy); else ctx.lineTo(cx, cy);
    });
    ctx.stroke();

    // token strip
    const sy = py + ph + 10, sh = 14;
    const maxTok = Math.max(...rows.map(r => (r.input_tokens||0) + (r.output_tokens||0) + (r.cache_write||0) + (r.cache_read||0)), 1);
    rows.forEach((r, i) => {
      const t = (r.input_tokens||0) + (r.output_tokens||0) + (r.cache_write||0) + (r.cache_read||0);
      ctx.fillStyle = alpha(C.chart[1], 0.15 + 0.85 * (t / maxTok));
      ctx.fillRect(px + i * bw + (bw - barW) / 2, sy, barW, sh);
    });
    text(ctx, "tokens", px - 8, sy + 11, { size: 10, color: C.muted, align: "right" });
    const labelY = sy + sh + 16;
    text(ctx, tick(rows[0].bucket), px, labelY, { size: 10.5, color: C.muted });
    text(ctx, tick(rows[rows.length - 1].bucket), px + pw, labelY, { size: 10.5, color: C.muted, align: "right" });
    const peak = rows.reduce((m, r) => (r.cost || 0) > (m.cost || 0) ? r : m, rows[0]);
    text(ctx, `peak ${tick(peak.bucket)} · ${fmtMoney(peak.cost)}  ·  total ${fmtMoney(cum)}`, x + w / 2, labelY, { size: 10.5, color: C.muted, align: "center" });
    return h;
  }

  function drawHeatmap(ctx, y, data) {
    const x = PAD, w = W - PAD * 2;
    const rows = data.heatmap?.rows || [];
    if (!rows.length) {
      card(ctx, x, y, w, 90);
      label(ctx, "Activity heatmap", x + 18, y + 24);
      text(ctx, "No activity in window.", x + 18, y + 60, { size: 12, color: C.muted });
      return 90;
    }
    const byDay = new Map(rows.map(r => [r.day, r]));
    // Same span as the page: the local days the view covers (see windowSpan in app.js).
    const span = window.__dash.windowSpan(rows[0].day);
    const metric = data.filters.heatMetric === "tokens" ? "tokens" : "cost";
    const first = new Date(span.from);
    first.setDate(first.getDate() - (first.getDay() + 6) % 7);
    const days = [];
    for (let d = new Date(first); d <= span.to; d.setDate(d.getDate() + 1)) {
      const iso = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
      days.push({ date: new Date(d), iso, inSpan: d >= span.from, ...byDay.get(iso) });
    }
    const cols = Math.ceil(days.length / 7);
    const padL = 30, padT = 44;
    const availW = w - 36 - padL;
    const cell = Math.max(8, Math.min(18, Math.floor((availW / cols) / 1.22)));
    const gut = Math.max(2, Math.round(cell * 0.22));
    const gridW = cols * (cell + gut);
    const ox = x + 18 + padL + Math.max(0, (availW - gridW) / 2);
    const oy = y + padT;
    const h = padT + 7 * (cell + gut) + 38;   // grid plus the summary line, never overlapping
    card(ctx, x, y, w, h);
    label(ctx, `Activity calendar · ${metric === "cost" ? "API-rate cost" : "tokens"} per day`, x + 18, y + 24);
    ["Mon","","Wed","","Fri","",""].forEach((lab, i) => {
      if (lab) text(ctx, lab, ox - 8, oy + i * (cell + gut) + cell - 2, { size: 9, color: C.muted, align: "right" });
    });
    // Month labels; the year rides on the first label and on each January when the span crosses years.
    const multiYear = span.from.getFullYear() !== span.to.getFullYear();
    const mon = new Intl.DateTimeFormat(undefined, { month: "short" }), mony = new Intl.DateTimeFormat(undefined, { month: "short", year: "numeric" });
    let lastMonth = -1, lastX = -1e9, firstLabel = true;
    days.forEach((d, idx) => {
      const mx = ox + Math.floor(idx / 7) * (cell + gut);
      if (d.date.getDate() <= 7 && d.date.getMonth() !== lastMonth) {
        lastMonth = d.date.getMonth();
        const withYear = multiYear && (firstLabel || d.date.getMonth() === 0);
        if (mx - lastX < (withYear ? 56 : 30)) return;
        lastX = mx; firstLabel = false;
        text(ctx, (withYear ? mony : mon).format(d.date), mx, oy - 8, { size: 10, weight: 500, color: C.muted });
      }
    });
    let activeDays = 0, totalCost = 0, peak = null;
    const max = days.reduce((m, d) => Math.max(m, d.inSpan ? d[metric] || 0 : 0), 0);
    days.forEach((d, idx) => {
      if (d.inSpan && d.msgs > 0) { activeDays++; totalCost += d.cost || 0; if (!peak || (d.cost || 0) > (peak.cost || 0)) peak = d; }
      ctx.globalAlpha = d.inSpan ? 1 : .35;
      ctx.fillStyle = heatColor(d.inSpan ? d[metric] || 0 : 0, max);
      rrect(ctx, ox + Math.floor(idx / 7) * (cell + gut), oy + (idx % 7) * (cell + gut), cell, cell, Math.max(2, Math.round(cell * 0.18)));
      ctx.fill();
      ctx.globalAlpha = 1;
    });
    const fy = y + h - 20;
    const peakStr = peak ? `busiest ${fmtDay(peak.iso)} · ${fmtMoney(peak.cost)}` : "";
    text(ctx, [`Active on ${activeDays} of ${span.days} day${span.days === 1 ? "" : "s"}`, peakStr,
               activeDays ? `${fmtMoney(totalCost / activeDays)} per active day` : ""].filter(Boolean).join("  ·  "), x + 18, fy, { size: 11, color: C.text2 });
    let lx = x + w - 18;
    text(ctx, "more", lx, fy, { size: 10, color: C.muted, align: "right" });
    lx -= 40;
    for (let i = 5; i >= 0; i--) { ctx.fillStyle = C.heat[i]; rrect(ctx, lx, fy - 9, 10, 10, 2); ctx.fill(); lx -= 13; }
    text(ctx, "less", lx + 8, fy, { size: 10, color: C.muted, align: "right" });
    return h;
  }

  // Model table with share-of-spend bars (matches the app's Models card).
  function drawByModel(ctx, y, data) {
    const x = PAD, w = W - PAD * 2;
    const all = (data.by_model?.rows || []).slice().sort((a, b) => b.cost - a.cost);
    const rows = all.slice(0, 6);
    const h = 64 + rows.length * 26 + 8;
    card(ctx, x, y, w, h);
    label(ctx, "Models · share of spend", x + 18, y + 24);
    if (!rows.length) return h;
    const total = all.reduce((s, r) => s + (r.cost || 0), 0) || 1;
    const hasFast = all.some(r => (r.fast_cost_usd || 0) > 0);
    const slots = window.__dash?.modelSlots ? window.__dash.modelSlots(all.filter(r => r.cost > 0).map(r => r.model)) : new Map();
    const sel = data.filters.model;
    const colTok = x + 330, colCost = x + 450, barX = x + 480, barW = w - (barX - x) - (hasFast ? 210 : 90);
    const hy = y + 48;
    text(ctx, "MODEL", x + 18, hy, { size: 9.5, weight: 600, color: C.muted });
    text(ctx, "TOKENS", colTok, hy, { size: 9.5, weight: 600, color: C.muted, align: "right" });
    text(ctx, "COST", colCost, hy, { size: 9.5, weight: 600, color: C.muted, align: "right" });
    if (hasFast) text(ctx, "FAST", x + w - 18, hy, { size: 9.5, weight: 600, color: C.muted, align: "right" });
    rows.forEach((r, i) => {
      const yy = hy + 24 + i * 26;
      const slot = slots.get(r.model);
      const col = alpha(slot != null ? C.chart[slot] : C.other, sel && sel !== r.model ? 0.35 : 1);
      ctx.fillStyle = col; rrect(ctx, x + 18, yy - 9, 10, 10, 3); ctx.fill();
      text(ctx, ellipsize(ctx, shortModel(r.model), 260, { size: 12.5, weight: 500 }), x + 36, yy, { size: 12.5, weight: 500 });
      text(ctx, fmtCompact(r.tokens), colTok, yy, { size: 12, color: C.text2, align: "right" });
      text(ctx, fmtMoney(r.cost), colCost, yy, { size: 12, color: C.text2, align: "right" });
      const pct = (r.cost || 0) / total;
      fill(ctx, barX, yy - 7, barW, 6, 3, C.track);
      if (pct > 0) fill(ctx, barX, yy - 7, Math.max(3, barW * pct), 6, 3, col);
      text(ctx, (pct * 100).toFixed(1) + "%", barX + barW + 44, yy, { size: 11.5, color: C.text2, align: "right" });
      if (hasFast) text(ctx, r.fast_cost_usd > 0 ? fmtMoney(r.fast_cost_usd) : "—", x + w - 18, yy, { size: 12, color: C.text2, align: "right" });
    });
    return h;
  }

  // Where the spend went: main thread vs subagents as one 100% bar (the part whose
  // transcript was deleted is hatched and labelled, never dropped), and top skills.
  const GONE = "(transcript gone)";
  function drawBreakdown(ctx, y, data) {
    const side = data.bd_sidechain, skills = data.bd_skill;
    if (!side || !(side.total_cost > 0)) return 0;
    const x = PAD, w = W - PAD * 2;
    const total = side.total_cost;
    const skillRows = (skills?.rows || []).filter(r => r.value !== GONE && r.value !== "(none)" && r.cost > 0).slice(0, 6);
    const h = Math.max(160, 54 + skillRows.length * 26 + 18);
    card(ctx, x, y, w, h);
    const colW = (w - 18 * 3) / 2;
    // left: main vs subagent
    const lx = x + 18;
    label(ctx, "Where the spend went · main vs subagent", lx, y + 26);
    const by = Object.fromEntries((side.rows || []).map(r => [r.value, r.cost || 0]));
    const segs = [
      ["Main thread", by.main || 0, C.chart[0], false],
      ["Subagents", by.subagent || 0, C.chart[1], false],
      ["Not attributable (transcript deleted)", by[GONE] || 0, C.other, true],
    ].filter(s => s[1] > 0);
    const barY = y + 46, barH = 14;
    let bx = lx;
    const gap = 2, usable = colW - gap * (segs.length - 1);
    segs.forEach(([, v, col, hatched], i) => {
      const sw = Math.max(2, usable * v / total);
      ctx.fillStyle = hatched ? hatch(ctx, col) : col;
      rrect(ctx, bx, barY, sw, barH, i === 0 || i === segs.length - 1 ? 4 : 0); ctx.fill();
      bx += sw + gap;
    });
    segs.forEach(([name, v, col, hatched], i) => {
      const ly = barY + barH + 26 + i * 24;
      ctx.fillStyle = hatched ? hatch(ctx, col) : col; rrect(ctx, lx, ly - 9, 10, 10, 3); ctx.fill();
      text(ctx, name, lx + 18, ly, { size: 12.5, weight: 500 });
      text(ctx, `${fmtMoney(v)}  ·  ${(v / total * 100).toFixed(1)}%`, lx + colW, ly, { size: 12, color: C.text2, align: "right" });
    });
    // right: top skills
    const rx = lx + colW + 18;
    label(ctx, "Top skills · API-rate cost", rx, y + 26);
    if (!skillRows.length) { text(ctx, "No skill use recorded in window.", rx, y + 58, { size: 12, color: C.muted }); return h; }
    const max = Math.max(...skillRows.map(r => r.cost), 0.01);
    const nameW = 170, valW = 120, trackX = rx + nameW, trackW = colW - nameW - valW;
    skillRows.forEach((r, i) => {
      const yy = y + 54 + i * 26;
      text(ctx, ellipsize(ctx, r.value, nameW - 12, { size: 12.5, weight: 500 }), rx, yy + 10, { size: 12.5, weight: 500 });
      fill(ctx, trackX, yy, trackW, 12, 4, C.track);
      fill(ctx, trackX, yy, Math.max(3, (r.cost / max) * trackW), 12, 4, C.chart[0]);
      text(ctx, fmtMoney(r.cost), rx + colW, yy + 10, { size: 11.5, color: C.text2, align: "right" });
    });
    return h;
  }

  function drawTopProjects(ctx, y, data) {
    const x = PAD, w = W - PAD * 2;
    const rows = (data.by_project?.rows || []).slice(0, 8);
    const h = 56 + rows.length * 30;
    card(ctx, x, y, w, h);
    label(ctx, "Top projects · tokens", x + 18, y + 24);
    if (!rows.length) { text(ctx, "No projects in window.", x + 18, y + 50, { size: 12, color: C.muted }); return h; }
    const max = Math.max(...rows.map(r => r.tokens), 1);
    const labelW = 220, valW = 150, padX = 18;
    const trackX = x + padX + labelW, trackW = w - padX * 2 - labelW - valW;
    rows.forEach((r, i) => {
      const yy = y + 46 + i * 30;
      text(ctx, ellipsize(ctx, r.project, labelW - 12, { size: 12.5, weight: 500 }), x + padX, yy + 10, { size: 12.5, weight: 500 });
      fill(ctx, trackX, yy, trackW, 12, 4, C.track);
      fill(ctx, trackX, yy, Math.max(3, (r.tokens / max) * trackW), 12, 4, C.chart[0]);
      text(ctx, fmtCompact(r.tokens) + " · " + fmtMoney(r.cost), x + w - padX, yy + 10, { size: 11.5, color: C.text2, align: "right" });
    });
    return h;
  }

  function drawTopSessions(ctx, y, data) {
    const x = PAD, w = W - PAD * 2;
    const rows = (data.top_sessions?.rows || []).slice(0, 6);
    const h = 60 + rows.length * 28;
    card(ctx, x, y, w, h);
    label(ctx, "Longest conversations", x + 18, y + 24);
    const hy = y + 46;
    const head = (s, xx, align = "left") => text(ctx, s, xx, hy, { size: 9.5, weight: 600, color: C.muted, align });
    head("PROJECT", x + 18); head("MODEL", x + 380);
    head("MSGS", x + w - 300, "right"); head("TOKENS", x + w - 210, "right"); head("COST", x + w - 110, "right"); head("DURATION", x + w - 18, "right");
    rows.forEach((r, i) => {
      const yy = hy + 22 + i * 28;
      text(ctx, ellipsize(ctx, r.project, 340, { size: 12.5, weight: 500 }), x + 18, yy, { size: 12.5, weight: 500 });
      text(ctx, ellipsize(ctx, shortModel(r.model), 200, { size: 12 }), x + 380, yy, { size: 12, color: C.text2 });
      text(ctx, fmtInt(r.msgs), x + w - 300, yy, { size: 12, color: C.text2, align: "right" });
      text(ctx, fmtCompact(r.tokens), x + w - 210, yy, { size: 12, color: C.text2, align: "right" });
      text(ctx, fmtMoney(r.cost), x + w - 110, yy, { size: 12, color: C.text2, align: "right" });
      const dm = r.duration_minutes || 0;
      text(ctx, dm < 60 ? dm + "m" : Math.floor(dm/60) + "h " + (dm%60) + "m", x + w - 18, yy, { size: 12, color: C.text2, align: "right" });
    });
    return h;
  }

  function drawFooter(ctx, y) {
    const x = PAD, w = W - PAD * 2, h = 30;
    text(ctx, "claude-usage-dashboard · github.com/ajmalaksar25/claude-usage-dashboard", x, y + 18, { size: 11, color: C.muted });
    const msg = "Generated locally · no data left this device";
    ctx.font = `400 11px ${SANS}`;
    const mw = ctx.measureText(msg).width;
    ctx.fillStyle = C.good;
    ctx.beginPath(); ctx.arc(x + w - mw - 10, y + 14, 3.5, 0, Math.PI * 2); ctx.fill();
    text(ctx, msg, x + w, y + 18, { size: 11, color: C.text2, align: "right" });
    return h;
  }

  // ---------- public ----------
  async function fetchAll(mode) {
    // Same helper as the app, so the account and project filters apply.
    const api = window.__dash?.api;
    if (!api) throw new Error("dashboard not initialised");
    // api() turns `window` into from/to on a drilled-in day and adds the model filter.
    const w = STATE.window;
    const hourly = window.__dash.isHourly();
    const [summary, timeseries, heatmap, byModel, byProject, topSessions] = await Promise.all([
      api("/api/summary", { window: w }),
      api("/api/timeseries", { window: w, bucket: hourly ? "hour" : "day" }),
      api("/api/heatmap", { window: w }),
      api("/api/by_model", { window: w }, { skipModel: true }),
      api("/api/by_project", { window: w, limit: 10 }),
      api("/api/top_sessions", { window: w, by: "tokens", limit: 6 }),
    ]);
    // The breakdown is optional: an older server without /api/breakdown just skips the section.
    const [bdSide, bdSkill] = mode === "full" ? await Promise.all([
      api("/api/breakdown", { window: w, dim: "sidechain" }).catch(() => null),
      api("/api/breakdown", { window: w, dim: "skill", limit: 12 }).catch(() => null),
    ]) : [null, null];
    return { summary, timeseries, heatmap, by_model: byModel, by_project: byProject, top_sessions: topSessions,
             bd_sidechain: bdSide, bd_skill: bdSkill,
             filters: { window: w, day: STATE.day, hourly, model: STATE.model, account: STATE.account, project: STATE.project,
                        heatMetric: STATE.heatMetric, list: window.__dash.activeFilters(),
                        range: window.__dash.rangeText(), windowName: window.__dash.viewLabel() } };
  }

  async function renderExport(mode) {
    readPalette();
    try { await document.fonts.ready; } catch {}
    const data = await fetchAll(mode);
    const sections = mode === "full"
      ? [drawHeader, drawKPIs, drawSavingsHero, drawDaily, ...(window.__dash.isHourly() ? [] : [drawHeatmap]), drawByModel, drawBreakdown, drawTopProjects, drawTopSessions, drawFooter]
      : [drawHeader, drawKPIs, drawSavingsHero, drawDaily, drawFooter];
    const probe = makeCanvas(W, 10, 1).ctx;
    const heights = sections.map(fn => fn(probe, 0, data, mode));
    const totalH = PAD + heights.reduce((s, h) => s + (h ? h + 12 : 0), 0) - 12 + PAD;
    const { canvas, ctx } = makeCanvas(W, totalH);
    fill(ctx, 0, 0, W, totalH, 0, C.bg);
    let y = PAD;
    sections.forEach((fn, i) => { if (!heights[i]) return; fn(ctx, y, data, mode); y += heights[i] + 12; });
    return canvas;
  }

  function downloadCanvas(canvas, name) {
    const a = document.createElement("a");
    a.href = canvas.toDataURL("image/png"); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
  }

  window.__exporter = {
    render: renderExport,
    async run(mode) {
      const canvas = await renderExport(mode);
      const stamp = new Date().toISOString().slice(0, 10);
      const view = STATE.day ? STATE.day : STATE.window === "custom" ? `${STATE.from}_${STATE.to}` : STATE.window;
      const slug = s => String(s).toLowerCase().replace(/[^a-z0-9.]+/g, "-").replace(/^-|-$/g, "").slice(0, 40);
      const extra = [STATE.model && slug(window.__dash.shortModel(STATE.model)), STATE.project && slug(STATE.project), STATE.account && slug(STATE.account)].filter(Boolean);
      downloadCanvas(canvas, `claude-usage-${mode === "full" ? "full" : "highlights"}-${[view, ...extra].join("-")}-${stamp}.png`);
    },
  };
})();
