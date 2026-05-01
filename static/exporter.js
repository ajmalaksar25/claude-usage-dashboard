// Canvas-based image exporter. Builds a poster-style PNG from scratch
// (no DOM scraping, no html2canvas) so output is consistent across
// browsers/OSes and renders correctly even when Chart.js canvases
// would otherwise come out blank.
//
// Two modes: "highlights" (KPIs + savings + sparkline) and "full"
// (adds heatmap, model split, top projects, top conversations).

(() => {
  const W = 1200;
  const PAD = 28;

  // Brand palette — locked here so the export style is independent of
  // CSS variables that may not be readable from a detached canvas.
  const C = {
    bg: "#0b0d10",
    panel: "#14181d",
    panel2: "#1b2026",
    panel3: "#232932",
    border: "#262b33",
    borderSoft: "#1c2127",
    text: "#ecedef",
    text2: "#c4c8cf",
    muted: "#8a93a3",
    accent: "#d97757",
    accent2: "#c46c4a",
    good: "#4ade80",
    chart: ["#d97757", "#6ea8fe", "#a78bfa", "#4ade80", "#fbbf24", "#f472b6", "#34d399", "#94a3b8"],
    heat: ["#181c22", "#3b261a", "#5a341e", "#9c4d23", "#c46938", "#e07a4f"],
  };

  const FAM = '"Inter", -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';

  // Official Claude mark — drawn as Path2D so it scales without rasterization.
  const CLAUDE_MARK_PATH = "m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z";
  const CLAUDE_MARK = (typeof Path2D !== "undefined") ? new Path2D(CLAUDE_MARK_PATH) : null;

  function drawClaudeMark(ctx, cx, cy, size, color, glow = true) {
    ctx.save();
    if (glow) {
      ctx.shadowColor = "rgba(217,119,87,0.55)";
      ctx.shadowBlur = 16;
    }
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
  const fmtDay = s => s ? new Date(s + "T00:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—";

  // ---------- canvas utilities ----------
  function makeCanvas(w, h, dpr=2) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.textBaseline = "alphabetic";
    return { canvas, ctx };
  }

  function rrect(ctx, x, y, w, h, r) {
    const rr = Math.min(r, w/2, h/2);
    ctx.beginPath();
    ctx.moveTo(x+rr, y);
    ctx.arcTo(x+w, y, x+w, y+h, rr);
    ctx.arcTo(x+w, y+h, x, y+h, rr);
    ctx.arcTo(x, y+h, x, y, rr);
    ctx.arcTo(x, y, x+w, y, rr);
    ctx.closePath();
  }

  function fill(ctx, x, y, w, h, r, color) {
    ctx.fillStyle = color;
    rrect(ctx, x, y, w, h, r);
    ctx.fill();
  }

  function stroke(ctx, x, y, w, h, r, color, lw=1) {
    ctx.strokeStyle = color; ctx.lineWidth = lw;
    rrect(ctx, x, y, w, h, r);
    ctx.stroke();
  }

  function text(ctx, str, x, y, opts = {}) {
    const { size = 13, weight = 400, color = C.text, family = FAM, align = "left", baseline = "alphabetic" } = opts;
    ctx.font = `${weight} ${size}px ${family}`;
    ctx.fillStyle = color;
    ctx.textAlign = align;
    ctx.textBaseline = baseline;
    ctx.fillText(str, x, y);
  }

  // truncate to fit a width, append "…"
  function ellipsize(ctx, str, maxW, opts = {}) {
    const { size = 13, weight = 400 } = opts;
    ctx.font = `${weight} ${size}px ${FAM}`;
    if (ctx.measureText(str).width <= maxW) return str;
    let s = str;
    while (s.length > 2 && ctx.measureText(s + "…").width > maxW) s = s.slice(0, -1);
    return s + "…";
  }

  // ---------- color helpers ----------
  function alpha(hex, a) {
    const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
    return `rgba(${r},${g},${b},${a})`;
  }

  function heatColor(v, max) {
    if (!v || max <= 0) return C.heat[0];
    const t = Math.log10(1 + v) / Math.log10(1 + max);
    const i = Math.min(C.heat.length - 1, Math.max(1, Math.ceil(t * (C.heat.length - 1))));
    return C.heat[i];
  }

  // ---------- section drawers ----------
  function drawHeader(ctx, y, data, mode) {
    const x = PAD, w = W - PAD * 2, h = 70;
    fill(ctx, x, y, w, h, 14, C.panel);
    // Claude mark (icon-only, accent color, soft glow)
    const markCx = x + 36, markCy = y + h / 2, markSize = 32;
    drawClaudeMark(ctx, markCx, markCy, markSize, C.accent, true);
    // Title
    const titleX = markCx + markSize / 2 + 14;
    text(ctx, "Claude Usage", titleX, y + 32, { size: 22, weight: 600, color: C.text });
    const sub = `${data.summary.window} · ${data.summary.first_ts ? new Date(data.summary.first_ts).toLocaleDateString() : ""} → ${data.summary.last_ts ? new Date(data.summary.last_ts).toLocaleDateString() : ""}`;
    text(ctx, sub, titleX, y + 52, { size: 12, color: C.muted });
    // Right side
    text(ctx, mode === "full" ? "Full report" : "Highlights", x + w - 16, y + 28, { size: 13, weight: 600, color: C.text2, align: "right" });
    text(ctx, "runs locally · no data leaves your machine", x + w - 16, y + 47, { size: 11, color: C.muted, align: "right" });
    text(ctx, `generated ${new Date().toLocaleString()}`, x + w - 16, y + 62, { size: 10.5, color: C.muted, align: "right" });
    return h;
  }

  function drawKPIs(ctx, y, data, mode) {
    const x = PAD, w = W - PAD * 2;
    const s = data.summary;
    const cards = [
      { k: "Total tokens", v: fmtCompact(s.total_tokens), sub: `in ${fmtCompact(s.input_tokens)} · out ${fmtCompact(s.output_tokens)} · cache ${fmtCompact(s.cache_5m_write + s.cache_1h_write + s.cache_read)}` },
      { k: "Messages", v: fmtInt(s.msgs), sub: `${fmtInt(s.sessions)} sessions · ${fmtInt(s.projects)} projects` },
      { k: "API cost", v: fmtMoney(s.api_cost), sub: "at standard API rates" },
    ];
    if (s.subscription_cost > 0) cards.push({ k: "Subscription paid", v: fmtMoney(s.subscription_cost), sub: (s.billing_charges||[]).map(c=>c.plan).filter((v,i,a)=>a.indexOf(v)===i).join(" · ") || "" });

    const n = cards.length;
    const gap = 12;
    const cw = (w - gap * (n - 1)) / n;
    const ch = 100;
    cards.forEach((c, i) => {
      const cx = x + i * (cw + gap);
      fill(ctx, cx, y, cw, ch, 12, C.panel);
      stroke(ctx, cx, y, cw, ch, 12, C.borderSoft);
      text(ctx, c.k.toUpperCase(), cx + 14, y + 22, { size: 10, weight: 600, color: C.muted });
      text(ctx, c.v, cx + 14, y + 56, { size: 26, weight: 600, color: C.text });
      if (c.sub) {
        const cropped = ellipsize(ctx, c.sub, cw - 28, { size: 11 });
        text(ctx, cropped, cx + 14, y + 80, { size: 11, color: C.muted });
      }
    });
    return ch;
  }

  function drawSavingsHero(ctx, y, data) {
    const s = data.summary;
    if (!(s.subscription_cost > 0)) return 0;
    const x = PAD, w = W - PAD * 2, h = 140;
    // Gradient bg
    const g = ctx.createLinearGradient(x, y, x + w, y + h);
    g.addColorStop(0, "#1f1411");
    g.addColorStop(1, "#1a1110");
    fill(ctx, x, y, w, h, 14, g);
    // Glow blob top-right
    const rg = ctx.createRadialGradient(x + w * 0.85, y + 10, 10, x + w * 0.85, y + 10, 280);
    rg.addColorStop(0, alpha(C.accent, 0.30));
    rg.addColorStop(1, alpha(C.accent, 0.00));
    fill(ctx, x, y, w, h, 14, rg);
    // Border
    stroke(ctx, x, y, w, h, 14, alpha(C.accent, 0.42), 1);

    text(ctx, "SAVED BY SUBSCRIBING", x + 22, y + 28, { size: 11, weight: 600, color: C.accent });
    const sav = s.savings;
    const savStr = (sav >= 0 ? "+" : "−") + fmtMoney(Math.abs(sav));
    text(ctx, savStr, x + 22, y + 80, { size: 52, weight: 700, color: C.accent });
    text(ctx, `vs ${fmtMoney(s.api_cost)} at standard API rates`, x + 22, y + 108, { size: 13, color: C.text2 });
    if (s.multiplier) {
      const badge = `${s.multiplier}× cheaper than API`;
      ctx.font = `600 12px ${FAM}`;
      const bw = ctx.measureText(badge).width + 22;
      const bx = x + w - bw - 22, by = y + 22, bh = 28;
      fill(ctx, bx, by, bw, bh, 999, alpha(C.accent, 0.16));
      stroke(ctx, bx, by, bw, bh, 999, alpha(C.accent, 0.32));
      text(ctx, badge, bx + bw / 2, by + 19, { size: 12, weight: 600, color: C.accent, align: "center" });
    }
    return h;
  }

  function drawSparkline(ctx, y, data) {
    const x = PAD, w = W - PAD * 2, h = 220;
    fill(ctx, x, y, w, h, 12, C.panel);
    stroke(ctx, x, y, w, h, 12, C.borderSoft);
    text(ctx, "DAILY ACTIVITY", x + 18, y + 22, { size: 10, weight: 600, color: C.muted });

    const rows = data.timeseries?.rows || [];
    if (!rows.length) {
      text(ctx, "No activity in window.", x + 18, y + 60, { size: 12, color: C.muted });
      return h;
    }

    // Top-right legend swatches: tokens (bar) + cumulative cost (line)
    const legY = y + 18;
    let legX = x + w - 18;
    ctx.font = `500 11px ${FAM}`;
    const tokensLabel = "tokens / day";
    const costLabel = "cumulative cost ($)";
    const tokensLabelW = ctx.measureText(tokensLabel).width;
    const costLabelW = ctx.measureText(costLabel).width;
    // cost (drawn rightmost first, since we right-align)
    text(ctx, costLabel, legX, legY + 4, { size: 11, color: C.text2, align: "right" });
    legX -= costLabelW + 8;
    ctx.strokeStyle = C.chart[4]; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(legX - 16, legY); ctx.lineTo(legX, legY); ctx.stroke();
    ctx.fillStyle = C.chart[4];
    ctx.beginPath(); ctx.arc(legX - 8, legY, 2.5, 0, Math.PI * 2); ctx.fill();
    legX -= 16 + 14;
    // tokens swatch
    text(ctx, tokensLabel, legX, legY + 4, { size: 11, color: C.text2, align: "right" });
    legX -= tokensLabelW + 8;
    ctx.fillStyle = alpha(C.accent, 0.9);
    rrect(ctx, legX - 14, legY - 5, 14, 10, 2); ctx.fill();

    // Plot area: leave room for the date labels at the bottom
    const px = x + 18, py = y + 44, pw = w - 36, ph = h - 84;
    const max = Math.max(...rows.map(r => (r.input_tokens||0) + (r.output_tokens||0) + (r.cache_write||0) + (r.cache_read||0)), 1);
    const bw = pw / rows.length;
    const barW = Math.max(1, bw - 1);

    // Cumulative cost line points
    let cum = 0;
    const cumPts = rows.map(r => (cum += r.cost || 0));
    const maxCum = Math.max(...cumPts, 1);

    // Subtle baseline
    ctx.strokeStyle = C.borderSoft; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px, py + ph + 0.5); ctx.lineTo(px + pw, py + ph + 0.5); ctx.stroke();

    rows.forEach((r, i) => {
      const tot = (r.input_tokens||0) + (r.output_tokens||0) + (r.cache_write||0) + (r.cache_read||0);
      const bh = (tot / max) * ph;
      ctx.fillStyle = alpha(C.accent, 0.9);
      ctx.fillRect(px + i * bw, py + ph - bh, barW, bh);
    });

    ctx.strokeStyle = C.chart[4]; ctx.lineWidth = 2.25;
    ctx.beginPath();
    rows.forEach((_, i) => {
      const cx = px + i * bw + bw / 2;
      const cy = py + ph - (cumPts[i] / maxCum) * ph;
      if (i === 0) ctx.moveTo(cx, cy); else ctx.lineTo(cx, cy);
    });
    ctx.stroke();

    // Date labels — well inside the card with breathing room
    const labelY = py + ph + 18;
    text(ctx, rows[0].bucket, px, labelY, { size: 10.5, color: C.muted });
    text(ctx, rows[rows.length - 1].bucket, px + pw, labelY, { size: 10.5, color: C.muted, align: "right" });

    // Footer info line: peak + total
    const footY = py + ph + 36;
    text(ctx, `peak ${fmtCompact(max)} tokens · cumulative cost ${fmtMoney(maxCum)}`, x + w / 2, footY, { size: 10.5, color: C.muted, align: "center" });

    return h;
  }

  function drawHeatmap(ctx, y, data) {
    const x = PAD, w = W - PAD * 2, h = 220;
    fill(ctx, x, y, w, h, 12, C.panel);
    stroke(ctx, x, y, w, h, 12, C.borderSoft);
    text(ctx, "ACTIVITY HEATMAP", x + 18, y + 22, { size: 10, weight: 600, color: C.muted });

    const rows = data.heatmap?.rows || [];
    if (!rows.length) {
      text(ctx, "No activity in window.", x + 18, y + 60, { size: 12, color: C.muted });
      return h;
    }
    // Build day list
    const byDay = new Map(rows.map(r => [r.day, r]));
    const winDays = ({today:1,"1w":7,"15d":15,"1m":30,"3m":90,"6m":180,"1y":365})[STATE.window];
    const last = new Date();
    const first = new Date();
    if (winDays != null) first.setDate(last.getDate() - winDays + 1);
    else first.setTime(new Date(rows[0].day + "T00:00:00").getTime());
    const dow = (first.getDay() + 6) % 7;
    first.setDate(first.getDate() - dow);

    const days = [];
    for (let d = new Date(first); d <= last; d.setDate(d.getDate() + 1)) {
      const iso = d.toISOString().slice(0,10);
      days.push({ date: new Date(d), iso, ...byDay.get(iso) });
    }

    const cols = Math.ceil(days.length / 7);
    const padL = 30, padT = 36;
    const availW = w - 36 - padL;
    const cellMax = 18, cellMin = 8;
    const cell = Math.max(cellMin, Math.min(cellMax, Math.floor((availW / cols) / 1.22)));
    const gut = Math.max(2, Math.round(cell * 0.22));
    const gridW = cols * (cell + gut);
    const ox = x + 18 + padL + Math.max(0, (availW - gridW) / 2);
    const oy = y + padT;

    // Weekday labels
    ["Mon","","Wed","","Fri","",""].forEach((lab, i) => {
      if (lab) text(ctx, lab, x + 18, oy + i * (cell + gut) + cell - 2, { size: 9, color: C.muted });
    });
    // Month labels
    let lastMonth = -1;
    days.forEach((d, idx) => {
      const col = Math.floor(idx / 7);
      if (d.date.getDate() <= 7 && d.date.getMonth() !== lastMonth) {
        lastMonth = d.date.getMonth();
        text(ctx, d.date.toLocaleDateString(undefined, { month: "short" }) + " '" + String(d.date.getFullYear()).slice(2),
          ox + col * (cell + gut), y + 18, { size: 10, weight: 500, color: C.muted });
      }
    });

    // Cells
    let activeDays = 0, total = 0, peak = null;
    const max = days.reduce((m, d) => Math.max(m, d.tokens || 0), 0);
    days.forEach((d, idx) => {
      if (d.tokens) { activeDays++; total += d.tokens; if (!peak || d.tokens > peak.tokens) peak = d; }
      const col = Math.floor(idx / 7);
      const row = idx % 7;
      const cx = ox + col * (cell + gut);
      const cy = oy + row * (cell + gut);
      const r = Math.max(2, Math.round(cell * 0.18));
      ctx.fillStyle = heatColor(d.tokens || 0, max);
      rrect(ctx, cx, cy, cell, cell, r);
      ctx.fill();
    });

    // Footer summary line
    const fy = y + h - 22;
    const peakStr = peak ? `${fmtDay(peak.iso)} · ${fmtCompact(peak.tokens)}` : "—";
    text(ctx, `${activeDays}/${days.length} active days  ·  peak ${peakStr}  ·  daily avg ${activeDays ? fmtCompact(total/activeDays) : "—"}`, x + 18, fy, { size: 11, color: C.text2 });
    // Legend on right
    let lx = x + w - 18;
    text(ctx, "more", lx, fy, { size: 10, color: C.muted, align: "right" });
    lx -= 28;
    for (let i = C.heat.length - 1; i >= 0; i--) {
      ctx.fillStyle = C.heat[i];
      rrect(ctx, lx, fy - 9, 10, 10, 2);
      ctx.fill();
      lx -= 13;
    }
    text(ctx, "less", lx + 4, fy, { size: 10, color: C.muted, align: "right" });
    return h;
  }

  function drawByModel(ctx, y, data) {
    const x = PAD, w = W - PAD * 2, h = 180;
    fill(ctx, x, y, w, h, 12, C.panel);
    stroke(ctx, x, y, w, h, 12, C.borderSoft);
    text(ctx, "BY MODEL", x + 18, y + 22, { size: 10, weight: 600, color: C.muted });

    const rows = (data.by_model?.rows || []).slice();
    if (!rows.length) return h;
    // Donut left
    const cx = x + 100, cy = y + 100, r1 = 62, r0 = 36;
    const total = rows.reduce((s, r) => s + (r.tokens || 0), 0) || 1;
    let a0 = -Math.PI / 2;
    rows.forEach((row, i) => {
      const ang = (row.tokens / total) * Math.PI * 2;
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(a0) * r0, cy + Math.sin(a0) * r0);
      ctx.arc(cx, cy, r1, a0, a0 + ang);
      ctx.arc(cx, cy, r0, a0 + ang, a0, true);
      ctx.closePath();
      ctx.fillStyle = C.chart[i % C.chart.length];
      ctx.fill();
      a0 += ang;
    });
    text(ctx, "tokens", cx, cy + 4, { size: 11, color: C.muted, align: "center" });
    // Legend / table right
    const tx = x + 200, tyHeader = y + 44;
    text(ctx, "model", tx, tyHeader, { size: 10, weight: 600, color: C.muted });
    text(ctx, "tokens", x + w - 220, tyHeader, { size: 10, weight: 600, color: C.muted, align: "right" });
    text(ctx, "cost", x + w - 18, tyHeader, { size: 10, weight: 600, color: C.muted, align: "right" });
    rows.slice(0, 5).forEach((row, i) => {
      const yy = tyHeader + 22 + i * 22;
      ctx.fillStyle = C.chart[i % C.chart.length];
      rrect(ctx, tx, yy - 9, 10, 10, 3); ctx.fill();
      text(ctx, ellipsize(ctx, row.model, w - 280 - 80, { size: 12 }), tx + 18, yy, { size: 12, color: C.text });
      text(ctx, fmtCompact(row.tokens), x + w - 220, yy, { size: 12, color: C.text2, align: "right" });
      text(ctx, fmtMoney(row.cost), x + w - 18, yy, { size: 12, color: C.text2, align: "right" });
    });
    return h;
  }

  function drawTopProjects(ctx, y, data) {
    const x = PAD, w = W - PAD * 2;
    const rows = (data.by_project?.rows || []).slice(0, 8);
    const h = 56 + rows.length * 30;
    fill(ctx, x, y, w, h, 12, C.panel);
    stroke(ctx, x, y, w, h, 12, C.borderSoft);
    text(ctx, "TOP PROJECTS", x + 18, y + 22, { size: 10, weight: 600, color: C.muted });
    if (!rows.length) {
      text(ctx, "No projects in window.", x + 18, y + 50, { size: 12, color: C.muted });
      return h;
    }
    const max = rows[0].tokens || 1;
    const labelW = 200, valW = 120, padX = 18;
    const trackX = x + padX + labelW;
    const trackW = w - padX * 2 - labelW - valW;
    rows.forEach((r, i) => {
      const yy = y + 50 + i * 30;
      const name = ellipsize(ctx, r.project, labelW - 8, { size: 12, weight: 500 });
      text(ctx, name, x + padX, yy + 8, { size: 12, weight: 500, color: C.text });
      // Track
      fill(ctx, trackX, yy, trackW, 14, 7, C.panel3);
      const bw = (r.tokens / max) * trackW;
      const grad = ctx.createLinearGradient(trackX, 0, trackX + trackW, 0);
      grad.addColorStop(0, C.accent); grad.addColorStop(1, C.accent2);
      fill(ctx, trackX, yy, bw, 14, 7, grad);
      text(ctx, fmtCompact(r.tokens) + " · " + fmtMoney(r.cost), x + w - padX, yy + 9, { size: 11, color: C.text2, align: "right" });
    });
    return h;
  }

  function drawTopSessions(ctx, y, data) {
    const x = PAD, w = W - PAD * 2;
    const rows = (data.top_sessions?.rows || []).slice(0, 6);
    const h = 60 + rows.length * 30;
    fill(ctx, x, y, w, h, 12, C.panel);
    stroke(ctx, x, y, w, h, 12, C.borderSoft);
    text(ctx, "LONGEST CONVERSATIONS", x + 18, y + 22, { size: 10, weight: 600, color: C.muted });
    // header
    const hy = y + 44;
    text(ctx, "project", x + 18, hy, { size: 10, weight: 600, color: C.muted });
    text(ctx, "model", x + 360, hy, { size: 10, weight: 600, color: C.muted });
    text(ctx, "msgs", x + w - 280, hy, { size: 10, weight: 600, color: C.muted, align: "right" });
    text(ctx, "tokens", x + w - 200, hy, { size: 10, weight: 600, color: C.muted, align: "right" });
    text(ctx, "cost", x + w - 100, hy, { size: 10, weight: 600, color: C.muted, align: "right" });
    text(ctx, "duration", x + w - 18, hy, { size: 10, weight: 600, color: C.muted, align: "right" });
    rows.forEach((r, i) => {
      const yy = hy + 22 + i * 28;
      text(ctx, ellipsize(ctx, r.project, 320, { size: 12, weight: 500 }), x + 18, yy, { size: 12, weight: 500, color: C.text });
      text(ctx, ellipsize(ctx, r.model, 200, { size: 11 }), x + 360, yy, { size: 11, color: C.text2 });
      text(ctx, fmtInt(r.msgs), x + w - 280, yy, { size: 12, color: C.text2, align: "right" });
      text(ctx, fmtCompact(r.tokens), x + w - 200, yy, { size: 12, color: C.text2, align: "right" });
      text(ctx, fmtMoney(r.cost), x + w - 100, yy, { size: 12, color: C.text2, align: "right" });
      const dm = r.duration_minutes || 0;
      const dur = dm < 60 ? dm + "m" : Math.floor(dm/60) + "h " + (dm%60) + "m";
      text(ctx, dur, x + w - 18, yy, { size: 12, color: C.text2, align: "right" });
    });
    return h;
  }

  function drawFooter(ctx, y) {
    const x = PAD, w = W - PAD * 2, h = 36;
    text(ctx, "claude-usage-dashboard · github.com/ajmalaksar25/claude-usage-dashboard", x, y + 18, { size: 11, color: C.muted });
    // green dot + privacy
    ctx.fillStyle = C.good;
    ctx.beginPath(); ctx.arc(x + w - 218, y + 14, 3.5, 0, Math.PI * 2); ctx.fill();
    text(ctx, "Generated locally · no data left this device", x + w, y + 18, { size: 11, color: C.text2, align: "right" });
    return h;
  }

  // ---------- public ----------
  async function fetchAll() {
    const [summary, timeseries, heatmap, byModel, byProject, topSessions, topDays] = await Promise.all([
      fetch("/api/summary?window=" + STATE.window).then(r => r.json()),
      fetch("/api/timeseries?window=" + STATE.window + "&bucket=day").then(r => r.json()),
      fetch("/api/heatmap?window=" + STATE.window).then(r => r.json()),
      fetch("/api/by_model?window=" + STATE.window).then(r => r.json()),
      fetch("/api/by_project?window=" + STATE.window + "&limit=10").then(r => r.json()),
      fetch("/api/top_sessions?window=" + STATE.window + "&by=tokens&limit=6").then(r => r.json()),
      fetch("/api/top_days?window=" + STATE.window + "&limit=10").then(r => r.json()),
    ]);
    return { summary, timeseries, heatmap, by_model: byModel, by_project: byProject, top_sessions: topSessions, top_days: topDays };
  }

  async function renderExport(mode) {
    const data = await fetchAll();
    // Build the section list
    const sections = mode === "full"
      ? [drawHeader, drawKPIs, drawSavingsHero, drawSparkline, drawHeatmap, drawByModel, drawTopProjects, drawTopSessions, drawFooter]
      : [drawHeader, drawKPIs, drawSavingsHero, drawSparkline, drawFooter];

    // First pass: measure heights using a throwaway context
    const probe = makeCanvas(W, 8000, 1).ctx;
    let totalH = PAD;
    const heights = sections.map(fn => {
      const probeY = 0;
      // Draw to a dummy canvas to measure (sections don't read pixels back)
      const h = fn(probe, probeY, data, mode);
      return h;
    });
    heights.forEach(h => { totalH += h + 12; });
    totalH += PAD - 12;

    // Final render
    const { canvas, ctx } = makeCanvas(W, totalH);
    // Background
    fill(ctx, 0, 0, W, totalH, 0, C.bg);
    // Subtle ambient glow at top right
    const glow = ctx.createRadialGradient(W * 0.85, 0, 0, W * 0.85, 0, 600);
    glow.addColorStop(0, "rgba(217,119,87,0.05)");
    glow.addColorStop(1, "rgba(217,119,87,0)");
    fill(ctx, 0, 0, W, totalH, 0, glow);

    let y = PAD;
    sections.forEach((fn, i) => {
      fn(ctx, y, data, mode);
      y += heights[i] + 12;
    });
    return canvas;
  }

  function downloadCanvas(canvas, name) {
    const url = canvas.toDataURL("image/png");
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
  }

  // expose
  window.__exporter = {
    async run(mode) {
      const canvas = await renderExport(mode);
      const stamp = new Date().toISOString().slice(0, 10);
      const tag = mode === "full" ? "full" : "highlights";
      downloadCanvas(canvas, `claude-usage-${tag}-${STATE.window}-${stamp}.png`);
    }
  };
})();
