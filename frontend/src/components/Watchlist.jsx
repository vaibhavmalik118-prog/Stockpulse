import { useState } from "react";

export default function Watchlist({ tickers, selected, prices, onSelect, onAdd }) {
  const [input, setInput] = useState("");

  const submit = (e) => {
    e.preventDefault();
    const t = input.trim().toUpperCase();
    if (/^[A-Z][A-Z0-9.-]{0,9}$/.test(t)) {
      onAdd(t);
      setInput("");
    }
  };

  return (
    <aside className="panel watchlist">
      <h3>Watchlist</h3>
      <ul>
        {tickers.map((t) => {
          const p = prices[t];
          return (
            <li key={t}>
              <button className={t === selected ? "active" : ""} onClick={() => onSelect(t)}>
                <span className="sym">{t}</span>
                <span className="px">{p != null ? p.toFixed(2) : "—"}</span>
              </button>
            </li>
          );
        })}
      </ul>
      <form onSubmit={submit} className="add-ticker">
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Add ticker" maxLength={10} />
        <button type="submit">+</button>
      </form>
    </aside>
  );
}
