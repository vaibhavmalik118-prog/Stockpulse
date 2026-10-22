import { useEffect, useRef, useState } from "react";
import { bollinger, ema, resample, rsi, sma, vwap } from "../indicators";

const COLORS = {
  bg: "#0B1220",
  grid: "#1C2740",
  axis: "#8FA3BF",
  up: "#22C55E",
  down: "#EF4444",
  last: "#F5B83D",
  entry: "#8FA3BF",
  cross: "#5B6B87",
  tooltipBg: "rgba(19,28,46,0.95)",
  text: "#E6EDF7",
  sma: "#38BDF8",
  ema: "#C084FC",
  bb: "#64748B",
  vwap: "#FB923C",
  rsi: "#EAB308",
  order: "#60A5FA",
};
const RIGHT_AXIS = 64;
const BOTTOM_AXIS = 24;
const TOP_PAD = 12;

const TIMEFRAMES = [1, 5, 15];
const INDICATORS = [
  { id: "sma", label: "SMA 20", color: COLORS.sma },
  { id: "ema", label: "EMA 50", color: COLORS.ema },
  { id: "bb", label: "Bollinger", color: COLORS.bb },
  { id: "vwap", label: "VWAP", color: COLORS.vwap },
  { id: "vol", label: "Volume", color: COLORS.axis },
  { id: "rsi", label: "RSI 14", color: COLORS.rsi },
];
const PREFS_KEY = "stockpulse.chart";

const fmtTime = (s) => new Date(s * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtPrice = (p) => p.toFixed(p >= 1000 ? 1 : 2);

function niceStep(range, target) {
  const raw = range / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const n = raw / mag;
  return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
}

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(PREFS_KEY));
    if (p && TIMEFRAMES.includes(p.tf) && p.ind) return p;
  } catch {
    /* storage unavailable */
  }
  return { tf: 1, ind: { sma: true, vol: true } };
}

/** Draw a polyline through the non-null points of `series`. */
function line(ctx, series, xOf, y, color, width = 1.4) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  let pen = false;
  series.forEach((v, i) => {
    if (v == null) {
      pen = false;
      return;
    }
    if (pen) ctx.lineTo(xOf(i), y(v));
    else ctx.moveTo(xOf(i), y(v));
    pen = true;
  });
  ctx.stroke();
}

/**
 * Live candlestick chart drawn with the Canvas 2D API, with timeframes, indicators and
 * horizontal lines for the position (entry / stop-loss / target) and pending orders.
 * Candles live in a ref; `version` changes whenever a tick arrives, which triggers a redraw.
 */
export default function CandleChart({ candlesRef, version, emptyMessage, levels, orders }) {
  const wrapRef = useRef(null);
  const canvasRef = useRef(null);
  const hoverRef = useRef(null);
  const frameRef = useRef(0);
  const [prefs, setPrefs] = useState(loadPrefs);
  const stateRef = useRef({});
  stateRef.current = { levels, orders, prefs, emptyMessage };

  useEffect(() => {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      /* storage unavailable */
    }
  }, [prefs]);

  const draw = () => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const { levels: lv, orders: pending, prefs: pr, emptyMessage: empty } = stateRef.current;
    const dpr = window.devicePixelRatio || 1;
    const W = wrap.clientWidth;
    const H = wrap.clientHeight;
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.width = `${W}px`;
      canvas.style.height = `${H}px`;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = COLORS.bg;
    ctx.fillRect(0, 0, W, H);

    const all = resample(candlesRef.current, pr.tf);
    const plotW = W - RIGHT_AXIS;
    const fullH = H - BOTTOM_AXIS - TOP_PAD;
    const rsiH = pr.ind.rsi ? Math.round(fullH * 0.22) : 0;
    const plotH = fullH - (rsiH ? rsiH + 8 : 0);
    if (!all.length || plotW < 50 || plotH < 50) {
      ctx.fillStyle = COLORS.axis;
      ctx.font = "14px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(empty || "Waiting for prices…", W / 2, H / 2);
      return;
    }

    // indicators are computed on the whole history, then cut to the visible window
    const closes = all.map((c) => c.close);
    const ind = {};
    if (pr.ind.sma) ind.sma = sma(closes, 20);
    if (pr.ind.ema) ind.ema = ema(closes, 50);
    if (pr.ind.bb) ind.bb = bollinger(closes, 20, 2);
    if (pr.ind.vwap) ind.vwap = vwap(all);
    if (pr.ind.rsi) ind.rsi = rsi(closes, 14);

    const slot = 9; // px per candle
    const count = Math.max(10, Math.min(all.length, Math.floor(plotW / slot)));
    const from = Math.max(0, all.length - count);
    const candles = all.slice(from);
    const cut = (arr) => arr.slice(from);
    const step = plotW / count;
    const bodyW = Math.max(2, Math.min(14, step * 0.65));

    let lo = Infinity;
    let hi = -Infinity;
    for (const c of candles) {
      lo = Math.min(lo, c.low);
      hi = Math.max(hi, c.high);
    }
    const overlays = [];
    if (ind.sma) overlays.push({ s: cut(ind.sma), color: COLORS.sma });
    if (ind.ema) overlays.push({ s: cut(ind.ema), color: COLORS.ema });
    if (ind.vwap) overlays.push({ s: cut(ind.vwap), color: COLORS.vwap });
    if (ind.bb) {
      overlays.push({ s: cut(ind.bb.upper), color: COLORS.bb, dash: [3, 3] });
      overlays.push({ s: cut(ind.bb.mid), color: COLORS.bb });
      overlays.push({ s: cut(ind.bb.lower), color: COLORS.bb, dash: [3, 3] });
    }
    for (const o of overlays) for (const v of o.s) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }

    // position and pending-order lines (kept in view so they're always visible)
    const lines = [];
    if (lv) {
      lines.push({ price: lv.avg_cost, color: COLORS.entry, label: "ENTRY", dash: [2, 4] });
      if (lv.stop_loss != null) lines.push({ price: lv.stop_loss, color: COLORS.down, label: lv.trail_pct ? "TRAIL" : "SL", dash: [8, 4] });
      if (lv.take_profit != null) lines.push({ price: lv.take_profit, color: COLORS.up, label: "TP", dash: [8, 4] });
    }
    for (const o of pending || []) {
      lines.push({ price: o.price, color: COLORS.order, label: `${o.side.toUpperCase()} ${o.type.toUpperCase()} ×${o.quantity}`, dash: [5, 3] });
    }
    for (const l of lines) {
      lo = Math.min(lo, l.price);
      hi = Math.max(hi, l.price);
    }
    if (hi - lo < 1e-6) {
      hi += 0.5;
      lo -= 0.5;
    }
    const pad = (hi - lo) * 0.08;
    lo -= pad;
    hi += pad;
    const y = (p) => TOP_PAD + ((hi - p) / (hi - lo)) * plotH;
    const xOf = (i) => i * step + step / 2;

    // grid + price axis
    ctx.font = "11px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const gs = niceStep(hi - lo, 6);
    for (let p = Math.ceil(lo / gs) * gs; p <= hi; p += gs) {
      const yy = Math.round(y(p)) + 0.5;
      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, yy);
      ctx.lineTo(plotW, yy);
      ctx.stroke();
      ctx.fillStyle = COLORS.axis;
      ctx.fillText(fmtPrice(p), plotW + 8, yy);
    }

    // time axis
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const every = Math.max(1, Math.round(90 / step));
    candles.forEach((c, i) => {
      if (i % every !== 0) return;
      const xx = Math.round(xOf(i)) + 0.5;
      ctx.strokeStyle = COLORS.grid;
      ctx.beginPath();
      ctx.moveTo(xx, TOP_PAD);
      ctx.lineTo(xx, TOP_PAD + fullH);
      ctx.stroke();
      ctx.fillStyle = COLORS.axis;
      ctx.fillText(fmtTime(c.start), xx, TOP_PAD + fullH + 6);
    });

    // volume bars along the bottom of the price pane
    if (pr.ind.vol) {
      const maxV = Math.max(...candles.map((c) => c.volume || 0), 1);
      const volH = plotH * 0.18;
      ctx.globalAlpha = 0.35;
      candles.forEach((c, i) => {
        const h = ((c.volume || 0) / maxV) * volH;
        ctx.fillStyle = c.close >= c.open ? COLORS.up : COLORS.down;
        ctx.fillRect(Math.round(xOf(i) - bodyW / 2), TOP_PAD + plotH - h, Math.round(bodyW), h);
      });
      ctx.globalAlpha = 1;
    }

    // Bollinger fill under the candles
    if (ind.bb) {
      const up = cut(ind.bb.upper);
      const dn = cut(ind.bb.lower);
      ctx.fillStyle = "rgba(100,116,139,0.08)";
      ctx.beginPath();
      let started = false;
      up.forEach((v, i) => {
        if (v == null) return;
        if (started) ctx.lineTo(xOf(i), y(v));
        else ctx.moveTo(xOf(i), y(v));
        started = true;
      });
      for (let i = dn.length - 1; i >= 0; i--) if (dn[i] != null) ctx.lineTo(xOf(i), y(dn[i]));
      ctx.closePath();
      if (started) ctx.fill();
    }

    // candles
    candles.forEach((c, i) => {
      const color = c.close >= c.open ? COLORS.up : COLORS.down;
      const xx = xOf(i);
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(xx) + 0.5, y(c.high));
      ctx.lineTo(Math.round(xx) + 0.5, y(c.low));
      ctx.stroke();
      const top = y(Math.max(c.open, c.close));
      const h = Math.max(1, Math.abs(y(c.open) - y(c.close)));
      ctx.fillRect(Math.round(xx - bodyW / 2), top, Math.round(bodyW), h);
    });

    // indicator overlays
    for (const o of overlays) {
      ctx.setLineDash(o.dash || []);
      line(ctx, o.s, xOf, y, o.color);
    }
    ctx.setLineDash([]);

    // position / order lines
    for (const l of lines) {
      const yy = Math.round(y(l.price)) + 0.5;
      ctx.setLineDash(l.dash);
      ctx.strokeStyle = l.color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, yy);
      ctx.lineTo(plotW, yy);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = "bold 10px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillStyle = l.color;
      ctx.fillText(`${l.label} ${fmtPrice(l.price)}`, 6, yy - 3);
      ctx.fillRect(plotW + 2, yy - 8, RIGHT_AXIS - 4, 16);
      ctx.fillStyle = COLORS.bg;
      ctx.textBaseline = "middle";
      ctx.fillText(fmtPrice(l.price), plotW + 7, yy);
    }

    // last price line + label
    const last = candles[candles.length - 1];
    const ly = Math.round(y(last.close)) + 0.5;
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = COLORS.last;
    ctx.beginPath();
    ctx.moveTo(0, ly);
    ctx.lineTo(plotW, ly);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = COLORS.last;
    ctx.fillRect(plotW + 2, ly - 9, RIGHT_AXIS - 4, 18);
    ctx.fillStyle = COLORS.bg;
    ctx.font = "bold 11px system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(fmtPrice(last.close), plotW + 7, ly);

    // RSI pane
    let rsiY = null;
    if (ind.rsi) {
      const top = TOP_PAD + plotH + 8;
      rsiY = (v) => top + ((100 - v) / 100) * rsiH;
      ctx.fillStyle = "rgba(234,179,8,0.05)";
      ctx.fillRect(0, rsiY(70), plotW, rsiY(30) - rsiY(70));
      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      ctx.strokeRect(0.5, top + 0.5, plotW - 1, rsiH - 1);
      ctx.font = "10px system-ui, sans-serif";
      ctx.textBaseline = "middle";
      for (const lvl of [30, 70]) {
        const yy = Math.round(rsiY(lvl)) + 0.5;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(0, yy);
        ctx.lineTo(plotW, yy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = COLORS.axis;
        ctx.fillText(String(lvl), plotW + 8, yy);
      }
      const r = cut(ind.rsi);
      line(ctx, r, xOf, rsiY, COLORS.rsi);
      const lastR = r[r.length - 1];
      ctx.fillStyle = COLORS.rsi;
      ctx.textBaseline = "top";
      ctx.fillText(`RSI 14${lastR != null ? `  ${lastR.toFixed(1)}` : ""}`, 6, top + 4);
    }

    // crosshair + tooltip
    const hover = hoverRef.current;
    if (hover && hover.x < plotW && hover.y > TOP_PAD && hover.y < TOP_PAD + fullH) {
      const i = Math.min(candles.length - 1, Math.max(0, Math.floor(hover.x / step)));
      const c = candles[i];
      const cx = Math.round(xOf(i)) + 0.5;
      ctx.strokeStyle = COLORS.cross;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(cx, TOP_PAD);
      ctx.lineTo(cx, TOP_PAD + fullH);
      ctx.moveTo(0, hover.y + 0.5);
      ctx.lineTo(plotW, hover.y + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);

      const at = (arr) => (arr ? arr[from + i] : null);
      const rows = [
        [fmtTime(c.start), COLORS.text],
        [`O ${fmtPrice(c.open)}   H ${fmtPrice(c.high)}`, COLORS.text],
        [`L ${fmtPrice(c.low)}   C ${fmtPrice(c.close)}`, COLORS.text],
      ];
      if (pr.ind.vol) rows.push([`Vol ${Math.round(c.volume || 0).toLocaleString()}`, COLORS.axis]);
      if (at(ind.sma) != null) rows.push([`SMA20 ${fmtPrice(at(ind.sma))}`, COLORS.sma]);
      if (at(ind.ema) != null) rows.push([`EMA50 ${fmtPrice(at(ind.ema))}`, COLORS.ema]);
      if (ind.bb && at(ind.bb.mid) != null) rows.push([`BB ${fmtPrice(at(ind.bb.lower))} – ${fmtPrice(at(ind.bb.upper))}`, COLORS.axis]);
      if (at(ind.vwap) != null) rows.push([`VWAP ${fmtPrice(at(ind.vwap))}`, COLORS.vwap]);
      if (at(ind.rsi) != null) rows.push([`RSI ${at(ind.rsi).toFixed(1)}`, COLORS.rsi]);
      ctx.font = "12px system-ui, sans-serif";
      const tw = Math.max(...rows.map(([l]) => ctx.measureText(l).width)) + 20;
      const th = rows.length * 17 + 12;
      const tx = Math.min(plotW - tw - 6, cx + 12);
      const ty = TOP_PAD + 8;
      ctx.fillStyle = COLORS.tooltipBg;
      ctx.strokeStyle = COLORS.grid;
      ctx.beginPath();
      ctx.roundRect ? ctx.roundRect(tx, ty, tw, th, 6) : ctx.rect(tx, ty, tw, th);
      ctx.fill();
      ctx.stroke();
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      rows.forEach(([l, color], k) => {
        ctx.fillStyle = color;
        ctx.fillText(l, tx + 10, ty + 8 + k * 17);
      });
    }
  };

  const schedule = () => {
    cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(draw);
  };

  const orderKey = (orders || []).map((o) => `${o.id}:${o.price}`).join(",");
  useEffect(schedule, [version, emptyMessage, prefs, orderKey,
                       levels?.avg_cost, levels?.stop_loss, levels?.take_profit]);

  useEffect(() => {
    const ro = new ResizeObserver(schedule);
    ro.observe(wrapRef.current);
    return () => {
      ro.disconnect();
      cancelAnimationFrame(frameRef.current);
    };
  }, []);

  const onMove = (e) => {
    const r = canvasRef.current.getBoundingClientRect();
    hoverRef.current = { x: e.clientX - r.left, y: e.clientY - r.top };
    schedule();
  };
  const onLeave = () => {
    hoverRef.current = null;
    schedule();
  };

  const toggle = (id) => setPrefs((p) => ({ ...p, ind: { ...p.ind, [id]: !p.ind[id] } }));

  return (
    <>
      <div className="chart-tools">
        <div className="seg small-seg">
          {TIMEFRAMES.map((tf) => (
            <button key={tf} className={prefs.tf === tf ? "on" : ""} onClick={() => setPrefs((p) => ({ ...p, tf }))}>
              {tf}m
            </button>
          ))}
        </div>
        <div className="ind-toggles">
          {INDICATORS.map((i) => (
            <button key={i.id} className={`chip ind ${prefs.ind[i.id] ? "on" : ""}`} onClick={() => toggle(i.id)}
                    style={prefs.ind[i.id] ? { borderColor: i.color, color: i.color } : undefined}>
              {i.label}
            </button>
          ))}
        </div>
      </div>
      <div className="chart-wrap" ref={wrapRef}>
        <canvas ref={canvasRef} onMouseMove={onMove} onMouseLeave={onLeave} />
      </div>
    </>
  );
}
