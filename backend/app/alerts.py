"""Price alerts: kept in memory per ticker, checked on every tick, fired once."""
from __future__ import annotations

from typing import Optional


class AlertError(ValueError):
    pass


class AlertEngine:
    def __init__(self, db):
        self.db = db
        self._active: dict[str, dict[int, dict]] = {}

    async def load(self) -> None:
        self._active.clear()
        for a in await self.db.list_alerts(status="active"):
            self._active.setdefault(a["ticker"], {})[a["id"]] = a

    async def create(self, ticker: str, target: float, direction: str,
                     current_price: Optional[float] = None) -> dict:
        if current_price is not None:
            if direction == "above" and current_price >= target:
                raise AlertError(f"{ticker} is already above {target} (now {current_price:.2f}).")
            if direction == "below" and current_price <= target:
                raise AlertError(f"{ticker} is already below {target} (now {current_price:.2f}).")
        alert = await self.db.add_alert(ticker, target, direction)
        self._active.setdefault(ticker, {})[alert["id"]] = alert
        return alert

    async def delete(self, alert_id: int) -> bool:
        for alerts in self._active.values():
            alerts.pop(alert_id, None)
        return await self.db.delete_alert(alert_id)

    def check(self, ticker: str, price: float, ts: float) -> list[dict]:
        """Return (and deactivate) every alert on this ticker that the price has crossed.
        Only this ticker's alerts are examined, so the check stays cheap."""
        alerts = self._active.get(ticker)
        if not alerts:
            return []
        fired = []
        for alert_id, a in list(alerts.items()):
            hit = price >= a["target"] if a["direction"] == "above" else price <= a["target"]
            if hit:
                del alerts[alert_id]
                fired.append({**a, "status": "triggered", "triggered_at": ts, "triggered_price": price})
        return fired

    def active_count(self) -> int:
        return sum(len(v) for v in self._active.values())
