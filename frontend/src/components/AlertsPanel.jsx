import { useEffect, useState } from "react";
import { api } from "../api";

export default function AlertsPanel({ ticker, price, alerts, onChange, notify }) {
  const [target, setTarget] = useState("");
  const [direction, setDirection] = useState("above");
  const [error, setError] = useState(null);

  useEffect(() => {
    setTarget("");
    setError(null);
  }, [ticker]);

  const suggest = () => (price != null ? (direction === "above" ? price * 1.001 : price * 0.999).toFixed(2) : "");

  const create = async (e) => {
    e.preventDefault();
    setError(null);
    const value = Number(target || suggest());
    if (!value) return;
    try {
      await api.createAlert(ticker, value, direction);
      notify(`Alert set: ${ticker} ${direction} ${value.toFixed(2)}`, "info");
      setTarget("");
      onChange();
    } catch (err) {
      setError(err.message);
    }
  };

  const remove = async (id) => {
    await api.deleteAlert(id).catch(() => {});
    onChange();
  };

  const active = alerts.filter((a) => a.status === "active");
  const recent = alerts.filter((a) => a.status === "triggered").slice(0, 3);

  return (
    <section className="panel">
      <h3>Price alerts</h3>
      <form className="alert-form" onSubmit={create}>
        <select value={direction} onChange={(e) => setDirection(e.target.value)}>
          <option value="above">Above</option>
          <option value="below">Below</option>
        </select>
        <input type="number" step="0.01" min="0" value={target} placeholder={suggest() || "Target"}
               onChange={(e) => setTarget(e.target.value)} />
        <button type="submit" disabled={price == null && !target}>Set</button>
      </form>
      {error && <div className="notice error">{error}</div>}
      <ul className="alert-list">
        {active.length === 0 && <li className="muted small">No active alerts</li>}
        {active.map((a) => (
          <li key={a.id}>
            <span><b>{a.ticker}</b> {a.direction} {a.target.toFixed(2)}</span>
            <button className="link" onClick={() => remove(a.id)} title="Delete alert">✕</button>
          </li>
        ))}
        {recent.map((a) => (
          <li key={a.id} className="triggered">
            <span><b>{a.ticker}</b> {a.direction} {a.target.toFixed(2)}</span>
            <span className="small">triggered</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
