import { useCallback, useEffect, useRef, useState } from "react";

const MAX_CANDLES = 300;

/**
 * One WebSocket connection for the whole dashboard.
 * - subscribes to the selected ticker's room (and unsubscribes from the old one)
 * - keeps the candles in a ref (fast) and bumps `version` so the chart redraws
 * - reconnects with exponential backoff and re-subscribes automatically
 */
export function useMarketSocket(ticker, onEvent) {
  const wsRef = useRef(null);
  const tickerRef = useRef(ticker);
  const candlesRef = useRef([]);
  const eventRef = useRef(onEvent);
  eventRef.current = onEvent;

  const [version, setVersion] = useState(0);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState(null);
  const [price, setPrice] = useState(null);
  const [lastTickAt, setLastTickAt] = useState(null);
  const [history, setHistory] = useState({ source: null, message: null, loading: true });

  const bump = () => setVersion((v) => v + 1);

  const upsert = (candle) => {
    const list = candlesRef.current;
    const last = list[list.length - 1];
    if (last && last.start === candle.start) list[list.length - 1] = candle;
    else if (!last || candle.start > last.start) {
      list.push(candle);
      if (list.length > MAX_CANDLES) list.splice(0, list.length - MAX_CANDLES);
    }
  };

  const handle = useCallback((msg) => {
    switch (msg.type) {
      case "status":
        setStatus(msg);
        break;
      case "history":
        if (msg.ticker !== tickerRef.current) return;
        candlesRef.current = (msg.candles || []).slice(-MAX_CANDLES);
        setHistory({ source: msg.source, message: msg.message, loading: false });
        {
          const last = candlesRef.current[candlesRef.current.length - 1];
          setPrice(last ? last.close : null);
        }
        bump();
        break;
      case "tick":
        if (msg.ticker !== tickerRef.current) return;
        upsert(msg.candle);
        setPrice(msg.price);
        setLastTickAt(Date.now());
        eventRef.current?.({ type: "price", ticker: msg.ticker, price: msg.price });
        bump();
        break;
      case "candle":
        if (msg.ticker !== tickerRef.current) return;
        upsert(msg.candle);
        bump();
        break;
      case "alert":
        eventRef.current?.({ type: "alert", alert: msg.alert });
        break;
      case "exit":
      case "order":
      case "trail":
        eventRef.current?.(msg);
        break;
      case "error":
        eventRef.current?.({ type: "error", message: msg.message });
        break;
      default:
        break;
    }
  }, []);

  // open (and keep re-opening) the connection
  useEffect(() => {
    let stopped = false;
    let retry = 0;
    let timer;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      wsRef.current = ws;
      ws.onopen = () => {
        retry = 0;
        setConnected(true);
        if (tickerRef.current) ws.send(JSON.stringify({ type: "subscribe", ticker: tickerRef.current }));
      };
      ws.onmessage = (e) => {
        if (wsRef.current !== ws) return; // ignore a stale, replaced connection
        try {
          handle(JSON.parse(e.data));
        } catch {
          /* ignore malformed message */
        }
      };
      ws.onclose = () => {
        if (wsRef.current !== ws) return;
        setConnected(false);
        if (!stopped) timer = setTimeout(connect, Math.min(1000 * 2 ** retry++, 15000));
      };
      ws.onerror = () => ws.close();
    };

    connect();
    const ping = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send(JSON.stringify({ type: "ping" }));
    }, 25000);

    return () => {
      stopped = true;
      clearTimeout(timer);
      clearInterval(ping);
      wsRef.current?.close();
    };
  }, [handle]);

  // switch rooms when the selected ticker changes
  useEffect(() => {
    const prev = tickerRef.current;
    tickerRef.current = ticker;
    candlesRef.current = [];
    setPrice(null);
    setLastTickAt(null);
    setHistory({ source: null, message: null, loading: true });
    bump();
    const ws = wsRef.current;
    if (ws?.readyState === WebSocket.OPEN && prev !== ticker) {
      if (prev) ws.send(JSON.stringify({ type: "unsubscribe", ticker: prev }));
      ws.send(JSON.stringify({ type: "subscribe", ticker }));
    }
  }, [ticker]);

  return { candlesRef, version, connected, status, price, lastTickAt, history };
}
