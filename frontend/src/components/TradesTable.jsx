// trade.reason -> [label, css class, tooltip]
const TAGS = {
  stop_loss: ["SL", "sl", "Closed by stop-loss"],
  take_profit: ["TP", "tp", "Closed by target profit"],
  exit: ["EXIT", "ex", "Closed with the Exit button"],
  limit: ["LMT", "ty", "Filled limit order"],
  stop: ["STP", "ty", "Filled stop order"],
};

export default function TradesTable({ trades }) {
  return (
    <section className="panel trades">
      <h3>Recent trades</h3>
      {trades.length === 0 ? (
        <div className="muted small">No trades yet. Place a paper trade to get started.</div>
      ) : (
        <table>
          <thead><tr><th>Time</th><th>Side</th><th>Ticker</th><th>Qty</th><th>Price</th><th>Realised P&amp;L</th></tr></thead>
          <tbody>
            {trades.map((t) => (
              <tr key={t.id}>
                <td>{new Date(t.ts * 1000).toLocaleTimeString()}</td>
                <td className={t.side === "buy" ? "up" : "down"}>
                  {t.side.toUpperCase()}
                  {TAGS[t.reason] && <span className={`tag ${TAGS[t.reason][1]}`} title={TAGS[t.reason][2]}>{TAGS[t.reason][0]}</span>}
                </td>
                <td>{t.ticker}</td>
                <td>{t.quantity}</td>
                <td>{t.price.toFixed(2)}</td>
                <td className={t.realized_pnl > 0 ? "up" : t.realized_pnl < 0 ? "down" : ""}>
                  {t.realized_pnl == null ? "—" : t.realized_pnl.toFixed(2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
