import { useEffect, useRef, useState } from "react";

const SOURCE_LABEL = {
  stored: "saved candles",
  finnhub: "Finnhub",
  yfinance: "yfinance (backup)",
  demo: "simulated history",
  none: "no history",
};

export default function PriceHeader({ ticker, price, candles, history }) {
  const [flash, setFlash] = useState("");
  const prev = useRef(price);

  useEffect(() => {
    if (prev.current != null && price != null && price !== prev.current) {
      setFlash(price > prev.current ? "up" : "down");
      const t = setTimeout(() => setFlash(""), 350);
      prev.current = price;
      return () => clearTimeout(t);
    }
    prev.current = price;
  }, [price]);

  const first = candles[0];
  const change = price != null && first ? price - first.open : null;
  const pct = change != null && first.open ? (change / first.open) * 100 : null;
  const dir = change == null ? "" : change >= 0 ? "up" : "down";

  return (
    <div className="price-header">
      <div>
        <div className="ticker">{ticker}</div>
        <div className="sub">1-minute candles · history from {SOURCE_LABEL[history.source] || "…"}</div>
      </div>
      <div className="price-block">
        <div className={`price ${flash}`}>{price != null ? price.toFixed(2) : "—"}</div>
        {change != null && (
          <div className={`change ${dir}`}>
            {change >= 0 ? "+" : ""}
            {change.toFixed(2)} ({pct >= 0 ? "+" : ""}
            {pct.toFixed(2)}%) <span className="muted">over chart range</span>
          </div>
        )}
      </div>
    </div>
  );
}
