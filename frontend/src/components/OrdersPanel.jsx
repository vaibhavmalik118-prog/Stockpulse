import { useState } from "react";
import { api } from "../api";

/** Pending limit / stop orders waiting on the server, plus the most recent closed ones. */
export default function OrdersPanel({ orders, prices, onChange, notify }) {
  const [busy, setBusy] = useState(null);
  const open = orders.filter((o) => o.status === "open");
  const recent = orders.filter((o) => o.status !== "open").slice(0, 4);

  const cancel = async (o) => {
    setBusy(o.id);
    try {
      await api.cancelOrder(o.id);
      notify(`Cancelled ${o.type} ${o.side} ${o.ticker} @ ${o.price.toFixed(2)}`, "info");
      onChange();
    } catch (err) {
      notify(err.message, "error");
      onChange();
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="panel">
      <h3>Open orders</h3>
      {open.length === 0 ? (
        <div className="muted small">No pending limit / stop orders</div>
      ) : (
        <ul className="order-list">
          {open.map((o) => {
            const last = prices[o.ticker];
            const away = last != null ? ((o.price - last) / last) * 100 : null;
            return (
              <li key={o.id}>
                <span>
                  <b className={o.side === "buy" ? "up" : "down"}>{o.side.toUpperCase()}</b> {o.quantity} {o.ticker}
                  <span className="tag ty">{o.type}</span> @ {o.price.toFixed(2)}
                  {away != null && <span className="muted small"> ({away >= 0 ? "+" : ""}{away.toFixed(2)}%)</span>}
                  {(o.stop_loss || o.take_profit || o.trail_pct) && (
                    <span className="muted small">
                      {o.stop_loss ? ` · SL ${o.stop_loss.toFixed(2)}` : ""}
                      {o.take_profit ? ` · TP ${o.take_profit.toFixed(2)}` : ""}
                      {o.trail_pct ? ` · trail ${o.trail_pct}%` : ""}
                    </span>
                  )}
                </span>
                <button className="link small" disabled={busy === o.id} onClick={() => cancel(o)}>Cancel</button>
              </li>
            );
          })}
        </ul>
      )}
      {recent.length > 0 && (
        <ul className="order-list closed">
          {recent.map((o) => (
            <li key={o.id} title={o.note || ""}>
              <span className="small">
                {o.side.toUpperCase()} {o.quantity} {o.ticker} {o.type} @ {o.price.toFixed(2)}
              </span>
              <span className={`small status-${o.status}`}>
                {o.status}{o.fill_price ? ` @ ${o.fill_price.toFixed(2)}` : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
