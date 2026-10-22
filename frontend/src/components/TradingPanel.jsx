import { useEffect, useState } from "react";
import { api } from "../api";

function useCountdown(seconds, stamp) {
  const [left, setLeft] = useState(seconds);
  useEffect(() => {
    setLeft(seconds);
    if (!seconds) return undefined;
    const started = Date.now();
    const id = setInterval(() => {
      const l = Math.max(0, seconds - (Date.now() - started) / 1000);
      setLeft(l);
      if (l <= 0) clearInterval(id);
    }, 250);
    return () => clearInterval(id);
  }, [seconds, stamp]);
  return left;
}

const fmt = (s) => {
  const total = Math.ceil(s);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
};

const num = (v) => (v === "" || v == null || Number.isNaN(Number(v)) ? null : Number(v));
const signed = (v) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;

const ORDER_TYPES = [
  { id: "market", label: "Market", help: "Fills now at the live price." },
  { id: "limit", label: "Limit", help: "Buy at or below / sell at or above your price." },
  { id: "stop", label: "Stop", help: "Buy when price rises to / sell when it falls to your price." },
];

/** A price input with % presets and the profit / loss it would lock in for the current quantity. */
function ExitInput({ label, value, onChange, entry, qty, bad, hint, presets }) {
  const v = num(value);
  const pnl = v != null && entry != null ? (v - entry) * (Number(qty) || 0) : null;
  const pct = v != null && entry ? ((v - entry) / entry) * 100 : null;
  return (
    <div className="exit-input">
      <div className="row">
        <label>{label}</label>
        <input type="number" min="0" step="0.01" placeholder={hint} value={value}
               className={bad ? "invalid" : ""} onChange={(e) => onChange(e.target.value)} />
      </div>
      <div className="exit-meta">
        <span className="presets">
          {presets.map((p) => (
            <button key={p} type="button" className="chip" disabled={entry == null}
                    onClick={() => onChange((entry * (1 + p / 100)).toFixed(2))}>
              {p > 0 ? "+" : ""}{p}%
            </button>
          ))}
        </span>
        {bad ? (
          <span className="down small">must be {hint}</span>
        ) : pnl != null ? (
          <span className={`small ${pnl >= 0 ? "up" : "down"}`}>{pct >= 0 ? "+" : ""}{pct.toFixed(1)}% · {signed(pnl)}</span>
        ) : null}
      </div>
    </div>
  );
}

export default function TradingPanel({ ticker, price, risk, riskStamp, position, equity, cash, onTraded, notify }) {
  const [type, setType] = useState("market");
  const [limitPrice, setLimitPrice] = useState("");
  const [qty, setQty] = useState(1);
  const [stopLoss, setStopLoss] = useState("");
  const [target, setTarget] = useState("");
  const [trail, setTrail] = useState("");
  const [riskPct, setRiskPct] = useState("1");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const cooldown = useCountdown(risk?.cooldown_remaining || 0, riskStamp);

  useEffect(() => {
    setMessage(null);
    setLimitPrice("");
    setStopLoss("");
    setTarget("");
    setTrail("");
  }, [ticker]);

  // the price the order is expected to fill at
  const entry = type === "market" ? price : num(limitPrice);
  const quantity = Number(qty) || 0;
  const sl = num(stopLoss);
  const tp = num(target);
  const tr = num(trail);
  const slBad = sl != null && entry != null && sl >= entry;
  const tpBad = tp != null && entry != null && tp <= entry;
  const trBad = tr != null && (tr < 0.1 || tr > 50);
  const priceBad = type !== "market" && entry == null;

  // position sizing: risk a fixed % of equity between entry and stop-loss
  const effectiveStop = sl ?? (tr != null && entry != null ? entry * (1 - tr / 100) : null);
  const perShareRisk = entry != null && effectiveStop != null && effectiveStop < entry ? entry - effectiveStop : null;
  const riskBudget = equity != null && num(riskPct) ? (equity * num(riskPct)) / 100 : null;
  const maxAffordable = entry ? Math.floor((cash ?? 0) / entry) : 0;
  const sizedQty = perShareRisk && riskBudget ? Math.min(maxAffordable, Math.floor(riskBudget / perShareRisk)) : null;
  const rr = perShareRisk && tp != null && entry != null && tp > entry ? (tp - entry) / perShareRisk : null;

  const blocked = risk?.halted || (risk && risk.trades_today >= risk.daily_trade_limit) || cooldown > 0;
  const held = position?.quantity ?? 0;
  const openPnl = position && price != null ? (price - position.avg_cost) * held : null;

  const run = async (fn) => {
    setBusy(true);
    setMessage(null);
    try {
      await fn();
      onTraded();
    } catch (err) {
      setMessage({ text: err.message, code: err.code });
      if (err.code) onTraded(); // refresh risk status
    } finally {
      setBusy(false);
    }
  };

  const place = (side) =>
    run(async () => {
      const order = { ticker, side, quantity, type };
      if (type !== "market") order.price = entry;
      if (side === "buy") Object.assign(order, { stop_loss: sl, take_profit: tp, trail_pct: tr });
      const res = await api.placeOrder(order);
      if (res.order) {
        const o = res.order;
        notify(`${o.type.toUpperCase()} ${o.side} ${o.quantity} ${o.ticker} @ ${o.price.toFixed(2)} placed — waiting for price`, "info");
      } else {
        const t = res.trade;
        const pnl = t.realized_pnl != null ? ` · P&L ${signed(t.realized_pnl)}` : "";
        notify(`${side === "buy" ? "Bought" : "Sold"} ${t.quantity} ${t.ticker} @ ${t.price.toFixed(2)}${pnl}`, "ok");
      }
      if (side === "buy") {
        setStopLoss("");
        setTarget("");
        setTrail("");
      }
    });

  const exit = () =>
    run(async () => {
      const t = (await api.exitPosition(ticker)).trade;
      notify(`Exited ${t.quantity} ${t.ticker} @ ${t.price.toFixed(2)} · P&L ${signed(t.realized_pnl)}`,
             t.realized_pnl >= 0 ? "ok" : "error");
    });

  const typeHelp = ORDER_TYPES.find((o) => o.id === type).help;
  const entryLabel = entry != null ? entry.toFixed(2) : "";

  return (
    <section className="panel">
      <h3>Trade {ticker}</h3>

      <div className="seg" role="tablist">
        {ORDER_TYPES.map((o) => (
          <button key={o.id} className={type === o.id ? "on" : ""} onClick={() => setType(o.id)}>{o.label}</button>
        ))}
      </div>
      <div className="muted small help">{typeHelp}</div>

      {type !== "market" && (
        <div className="row">
          <label>{type === "limit" ? "Limit price" : "Stop price"}</label>
          <input type="number" min="0" step="0.01" placeholder={price != null ? price.toFixed(2) : ""}
                 value={limitPrice} onChange={(e) => setLimitPrice(e.target.value)} />
        </div>
      )}

      <div className="row">
        <label>Quantity</label>
        <div className="qty">
          <input type="number" min="1" max="100000" value={qty} onChange={(e) => setQty(e.target.value)} />
          <button className="chip" disabled={!maxAffordable} onClick={() => setQty(maxAffordable)} title="Most you can afford">Max</button>
          {held > 0 && <button className="chip" onClick={() => setQty(held)} title="Shares you hold">All {held}</button>}
        </div>
      </div>
      <div className="muted small">
        {entry != null ? `≈ $${(entry * quantity).toLocaleString(undefined, { maximumFractionDigits: 2 })} at ${entry.toFixed(2)}` : "Waiting for a price…"}
      </div>

      <div className="exits">
        <div className="exits-title">Risk management <span className="muted">(applies to buys)</span></div>
        <ExitInput label="Stop-loss" value={stopLoss} onChange={setStopLoss} entry={entry} qty={qty} bad={slBad}
                   hint={entryLabel ? `below ${entryLabel}` : ""} presets={[-1, -2, -5]} />
        <ExitInput label="Target" value={target} onChange={setTarget} entry={entry} qty={qty} bad={tpBad}
                   hint={entryLabel ? `above ${entryLabel}` : ""} presets={[2, 5, 10]} />
        <div className="row">
          <label title="Stop-loss follows the price up by this % and never moves down">Trailing stop %</label>
          <input type="number" min="0.1" max="50" step="0.1" placeholder="e.g. 2" value={trail}
                 className={trBad ? "invalid" : ""} onChange={(e) => setTrail(e.target.value)} />
        </div>
        <div className="row">
          <label title="How much of your equity you are willing to lose if the stop-loss is hit">Risk % of equity</label>
          <div className="qty">
            <input type="number" min="0.1" max="100" step="0.1" value={riskPct} onChange={(e) => setRiskPct(e.target.value)} />
            <button className="chip" disabled={!sizedQty} onClick={() => setQty(sizedQty)}
                    title="Set quantity so a stop-out loses exactly this % of equity">Size</button>
          </div>
        </div>
        <div className="muted small sizing">
          {sizedQty != null
            ? `Risking $${riskBudget.toFixed(2)} → ${sizedQty} share(s)${rr ? ` · reward:risk ${rr.toFixed(1)} : 1` : ""}`
            : "Set a stop-loss or trailing % to size the position by risk."}
        </div>
      </div>

      <div className="buttons">
        <button className="buy" disabled={busy || !entry || !quantity || blocked || slBad || tpBad || trBad || priceBad}
                onClick={() => place("buy")}>
          {type === "market" ? "Buy · Enter" : `Place buy ${type}`}
        </button>
        <button className="sell" disabled={busy || !entry || !quantity || blocked || priceBad} onClick={() => place("sell")}>
          {type === "market" ? "Sell" : `Place sell ${type}`}
        </button>
      </div>

      {held > 0 && (
        <button className="exit-btn" disabled={busy || price == null} onClick={exit}
                title="Sell the whole position now. Never blocked by the risk engine.">
          Exit position · {held} {ticker}
          {openPnl != null && <span className={openPnl >= 0 ? "up" : "down"}> {signed(openPnl)}</span>}
        </button>
      )}

      {message && <div className={`notice ${message.code ? "risk" : "error"}`}>{message.text}</div>}

      {risk && (
        <div className="risk">
          <div className="risk-title">Risk engine</div>
          <div className={`risk-item ${cooldown > 0 ? "warn" : ""}`}>
            <span>Cooldown</span>
            <b>{cooldown > 0 ? `${fmt(cooldown)} left` : "Ready"}</b>
          </div>
          <div className={`risk-item ${risk.trades_today >= risk.daily_trade_limit ? "bad" : ""}`}>
            <span>Trades today</span>
            <b>{risk.trades_today} / {risk.daily_trade_limit}</b>
          </div>
          <div className={`risk-item ${risk.halted ? "bad" : risk.loss_streak > 0 ? "warn" : ""}`}>
            <span>Losing streak</span>
            <b>{risk.halted ? "HALTED" : `${risk.loss_streak} / ${risk.max_consecutive_losses}`}</b>
          </div>
          {held > 0 && <div className="muted small">Exit, stop-loss and target sells are never blocked.</div>}
        </div>
      )}
    </section>
  );
}
