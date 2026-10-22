// Small wrapper around fetch for the StockPulse REST API.

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* empty body */
  }
  if (!res.ok) {
    const d = data && data.detail;
    let message = `Request failed (${res.status})`;
    if (typeof d === "string") message = d;
    else if (d && d.reason) message = d.reason;
    else if (Array.isArray(d)) message = d.map((x) => x.msg).join(", ");
    const err = new Error(message);
    err.code = d && d.code;
    err.retryAfter = d && d.retry_after;
    throw err;
  }
  return data;
}

const post = (path, body) => request(path, { method: "POST", body: JSON.stringify(body ?? {}) });

export const api = {
  config: () => request("/api/config"),
  portfolio: () => request("/api/portfolio"),
  trades: (limit = 20) => request(`/api/trades?limit=${limit}`),
  alerts: () => request("/api/alerts"),
  createAlert: (ticker, target, direction) => post("/api/alerts", { ticker, target, direction }),
  deleteAlert: (id) => request(`/api/alerts/${id}`, { method: "DELETE" }),
  // order: { ticker, side, quantity, type?, price?, stop_loss?, take_profit?, trail_pct? }
  placeOrder: (order) => post("/api/orders", order),
  orders: (status) => request(`/api/orders${status ? `?status=${status}` : ""}`),
  cancelOrder: (id) => request(`/api/orders/${id}`, { method: "DELETE" }),
  exitPosition: (ticker) => post(`/api/positions/${ticker}/exit`),
  setExits: (ticker, stop_loss, take_profit, trail_pct = null) =>
    request(`/api/positions/${ticker}/exits`, {
      method: "PUT",
      body: JSON.stringify({ stop_loss, take_profit, trail_pct }),
    }),
  resetAccount: () => post("/api/account/reset"),
};
