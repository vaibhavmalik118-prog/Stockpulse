import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useMarketSocket } from "./useMarketSocket";
import CandleChart from "./components/CandleChart";
import Watchlist from "./components/Watchlist";
import PriceHeader from "./components/PriceHeader";
import TradingPanel from "./components/TradingPanel";
import AlertsPanel from "./components/AlertsPanel";
import Portfolio from "./components/Portfolio";
import TradesTable from "./components/TradesTable";
import OrdersPanel from "./components/OrdersPanel";
import Toasts from "./components/Toasts";

const DEFAULT_TICKERS = ["AAPL", "TSLA", "MSFT", "NVDA", "AMZN", "GOOGL"];

export default function App() {
  const [tickers, setTickers] = useState(DEFAULT_TICKERS);
  const [selected, setSelected] = useState(DEFAULT_TICKERS[0]);
  const [prices, setPrices] = useState({});
  const [portfolio, setPortfolio] = useState(null);
  const [riskStamp, setRiskStamp] = useState(0);
  const [trades, setTrades] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [orders, setOrders] = useState([]);
  const [toasts, setToasts] = useState([]);
  const toastId = useRef(0);

  const notify = useCallback((text, kind = "info") => {
    const id = ++toastId.current;
    setToasts((t) => [...t.slice(-3), { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);

  const refreshAccount = useCallback(async () => {
    try {
      const [p, t, o] = await Promise.all([api.portfolio(), api.trades(15), api.orders()]);
      setPortfolio(p);
      setRiskStamp(Date.now());
      setTrades(t);
      setOrders(o);
    } catch {
      /* backend not reachable yet */
    }
  }, []);

  const refreshAlerts = useCallback(async () => {
    try {
      setAlerts(await api.alerts());
    } catch {
      /* ignore */
    }
  }, []);

  const onEvent = useCallback(
    (e) => {
      if (e.type === "price") {
        setPrices((p) => (p[e.ticker] === e.price ? p : { ...p, [e.ticker]: e.price }));
      } else if (e.type === "alert") {
        const a = e.alert;
        notify(`🔔 ${a.ticker} crossed ${a.direction} ${a.target.toFixed(2)} (now ${a.triggered_price.toFixed(2)})`, "alert");
        refreshAlerts();
      } else if (e.type === "exit") {
        const t = e.trade;
        const label = e.reason === "stop_loss" ? "🛑 Stop-loss hit" : e.reason === "take_profit" ? "🎯 Target hit" : "Exited";
        const pnl = `${t.realized_pnl >= 0 ? "+" : ""}${t.realized_pnl.toFixed(2)}`;
        notify(`${label}: sold ${t.quantity} ${t.ticker} @ ${t.price.toFixed(2)} · P&L ${pnl}`,
               e.reason === "stop_loss" ? "error" : "ok");
        refreshAccount();
      } else if (e.type === "order") {
        const o = e.order;
        if (e.status === "filled") {
          notify(`✅ ${o.type.toUpperCase()} ${o.side} filled: ${o.quantity} ${o.ticker} @ ${e.trade.price.toFixed(2)}`, "ok");
        } else {
          notify(`${o.type.toUpperCase()} ${o.side} ${o.ticker} rejected: ${o.note || "not filled"}`, "error");
        }
        refreshAccount();
      } else if (e.type === "trail") {
        // trailing stop moved: update it in place so the chart line follows immediately
        setPortfolio((p) => p && {
          ...p,
          positions: p.positions.map((x) => (x.ticker === e.ticker ? { ...x, stop_loss: e.stop_loss } : x)),
        });
      } else if (e.type === "error") {
        notify(e.message, "error");
      }
    },
    [notify, refreshAlerts, refreshAccount],
  );

  const market = useMarketSocket(selected, onEvent);

  // keep the watchlist price for the selected ticker in sync with history too
  useEffect(() => {
    if (market.price != null) setPrices((p) => ({ ...p, [selected]: market.price }));
  }, [market.price, selected]);

  useEffect(() => {
    api.config().then((c) => c?.tickers?.length && setTickers(c.tickers)).catch(() => {});
    refreshAccount();
    refreshAlerts();
    const id = setInterval(refreshAccount, 10000);
    return () => clearInterval(id);
  }, [refreshAccount, refreshAlerts]);

  const addTicker = (t) => {
    setTickers((list) => (list.includes(t) ? list : [...list, t]));
    setSelected(t);
  };

  const resetAccount = async () => {
    if (!window.confirm("Reset your virtual account? This clears all positions and trades.")) return;
    await api.resetAccount();
    notify("Virtual account reset", "info");
    refreshAccount();
  };

  const status = market.status;
  const position = portfolio?.positions.find((p) => p.ticker === selected);
  const tickerOrders = orders.filter((o) => o.status === "open" && o.ticker === selected);
  const candles = market.candlesRef.current;
  const emptyMessage = market.history.loading
    ? "Loading price history…"
    : market.history.message || "Waiting for live prices…";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">▮▮▮</span> StockPulse
          <span className="tagline">Real-time stock dashboard &amp; paper trading</span>
        </div>
        <div className="statusbar">
          {status && (
            <span className={`badge ${status.mode === "demo" ? "demo" : "live"}`} title={status.message}>
              {status.mode === "demo" ? "DEMO DATA" : "LIVE · FINNHUB"}
            </span>
          )}
          {status && (
            <span className={`badge ${status.market_open ? "open" : "closed"}`}>
              US market {status.market_open ? "open" : "closed"}
            </span>
          )}
          <span className={`conn ${market.connected ? "on" : "off"}`}>
            <i /> {market.connected ? "Connected" : "Reconnecting…"}
          </span>
        </div>
      </header>

      {status?.message && <div className="banner">{status.message}</div>}

      <main className="layout">
        <Watchlist tickers={tickers} selected={selected} prices={prices} onSelect={setSelected} onAdd={addTicker} />

        <div className="center">
          <section className="panel chart-panel">
            <PriceHeader ticker={selected} price={market.price} candles={candles} history={market.history} />
            <CandleChart candlesRef={market.candlesRef} version={market.version} emptyMessage={emptyMessage}
                         levels={position} orders={tickerOrders} />
          </section>
          <TradesTable trades={trades} />
        </div>

        <div className="side">
          <TradingPanel ticker={selected} price={market.price} risk={portfolio?.risk} riskStamp={riskStamp}
                        position={position} equity={portfolio?.equity} cash={portfolio?.cash}
                        onTraded={refreshAccount} notify={notify} />
          <OrdersPanel orders={orders} prices={prices} onChange={refreshAccount} notify={notify} />
          <AlertsPanel ticker={selected} price={market.price} alerts={alerts} onChange={refreshAlerts} notify={notify} />
          <Portfolio portfolio={portfolio} livePrices={prices} onReset={resetAccount}
                     onChange={refreshAccount} notify={notify} />
        </div>
      </main>

      <Toasts items={toasts} onClose={(id) => setToasts((t) => t.filter((x) => x.id !== id))} />
    </div>
  );
}
