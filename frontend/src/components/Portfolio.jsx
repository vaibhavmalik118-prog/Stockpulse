import { Fragment, useState } from "react";
import { api } from "../api";

const money = (v) =>
  v == null ? "—" : `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const num = (v) => (v === "" || Number.isNaN(Number(v)) ? null : Number(v));

/** Stop-loss / target / trailing line under a position, editable in place, plus the Exit button. */
function ExitLevels({ row, onChange, notify }) {
  const [editing, setEditing] = useState(false);
  const [sl, setSl] = useState("");
  const [tp, setTp] = useState("");
  const [trail, setTrail] = useState("");
  const [busy, setBusy] = useState(false);

  const open = () => {
    setSl(row.stop_loss != null ? String(row.stop_loss) : "");
    setTp(row.take_profit != null ? String(row.take_profit) : "");
    setTrail(row.trail_pct != null ? String(row.trail_pct) : "");
    setEditing(true);
  };

  const exit = async () => {
    setBusy(true);
    try {
      const t = (await api.exitPosition(row.ticker)).trade;
      const pnl = `${t.realized_pnl >= 0 ? "+" : "-"}$${Math.abs(t.realized_pnl).toFixed(2)}`;
      notify(`Exited ${t.quantity} ${t.ticker} @ ${t.price.toFixed(2)} · P&L ${pnl}`, t.realized_pnl >= 0 ? "ok" : "error");
      onChange();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const save = async (stopLoss, takeProfit, trailPct = null) => {
    setBusy(true);
    try {
      await api.setExits(row.ticker, stopLoss, takeProfit, trailPct);
      notify(stopLoss == null && takeProfit == null && trailPct == null ? `${row.ticker} auto-exit cleared`
                                                                        : `${row.ticker} auto-exit updated`, "ok");
      setEditing(false);
      onChange();
    } catch (err) {
      notify(err.message, "error");
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <div className="exit-edit">
        <input type="number" step="0.01" placeholder="Stop-loss" value={sl} onChange={(e) => setSl(e.target.value)} />
        <input type="number" step="0.01" placeholder="Target" value={tp} onChange={(e) => setTp(e.target.value)} />
        <input type="number" step="0.1" placeholder="Trail %" value={trail} onChange={(e) => setTrail(e.target.value)} />
        <button className="small" disabled={busy} onClick={() => save(num(sl), num(tp), num(trail))}>Save</button>
        <button className="link small" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
      </div>
    );
  }

  const none = row.stop_loss == null && row.take_profit == null;
  return (
    <div className="exit-line small">
      <span className="muted">SL</span> <b className="down">{row.stop_loss != null ? row.stop_loss.toFixed(2) : "—"}</b>
      <span className="muted">TP</span> <b className="up">{row.take_profit != null ? row.take_profit.toFixed(2) : "—"}</b>
      {row.trail_pct != null && <span className="muted" title="Trailing stop">↑{row.trail_pct}%</span>}
      <button className="link small" onClick={open}>{none ? "Set" : "Edit"}</button>
      {!none && <button className="link small" disabled={busy} onClick={() => save(null, null)}>Clear</button>}
      <button className="exit-mini" disabled={busy} onClick={exit} title="Sell the whole position now">Exit</button>
    </div>
  );
}

export default function Portfolio({ portfolio, livePrices, onReset, onChange, notify }) {
  if (!portfolio) return <section className="panel"><h3>Portfolio</h3><div className="muted">Loading…</div></section>;

  // use the freshest price we have for each position
  let mv = 0;
  let upnl = 0;
  const rows = portfolio.positions.map((p) => {
    const last = livePrices[p.ticker] ?? p.last_price ?? p.avg_cost;
    const value = last * p.quantity;
    const pnl = (last - p.avg_cost) * p.quantity;
    mv += value;
    upnl += pnl;
    return { ...p, last, value, pnl };
  });
  const equity = portfolio.cash + mv;

  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Portfolio</h3>
        <button className="link small" onClick={onReset} title="Reset virtual account">Reset</button>
      </div>
      <div className="stats">
        <div><span>Equity</span><b>{money(equity)}</b></div>
        <div><span>Cash</span><b>{money(portfolio.cash)}</b></div>
        <div><span>Unrealised</span><b className={cls(upnl)}>{money(upnl)}</b></div>
        <div><span>Realised</span><b className={cls(portfolio.realized_pnl)}>{money(portfolio.realized_pnl)}</b></div>
      </div>
      {rows.length > 0 ? (
        <table className="positions">
          <thead><tr><th>Ticker</th><th>Qty</th><th>Avg</th><th>Last</th><th>P&amp;L</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <Fragment key={r.ticker}>
                <tr className="pos-main">
                  <td>{r.ticker}</td><td>{r.quantity}</td><td>{r.avg_cost.toFixed(2)}</td>
                  <td>{r.last.toFixed(2)}</td><td className={cls(r.pnl)}>{r.pnl.toFixed(2)}</td>
                </tr>
                <tr className="pos-exits">
                  <td colSpan={5}><ExitLevels row={r} onChange={onChange} notify={notify} /></td>
                </tr>
              </Fragment>
            ))}
          </tbody>
        </table>
      ) : (
        <div className="muted small">No open positions</div>
      )}
    </section>
  );
}
