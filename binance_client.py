"""
binance_client.py — Binance Demo Futures (demo-fapi.binance.com)
================================================================
Kaynak: https://demo.binance.com/en/futures/BTCUSDT
API:    https://demo-fapi.binance.com  (USDⓈ-M Futures Demo)
WS:     wss://fstream.binancefuture.com

Klines   → public, key gerektirmez
Trade    → DEMO_KEY + DEMO_SECRET gerekir
         (demo.binance.com → API Management'tan alınır)

Ortam değişkenleri:
  BINANCE_DEMO_KEY     → Demo futures API key
  BINANCE_DEMO_SECRET  → Demo futures secret
  SYMBOL               → default ETHUSDT
"""

import asyncio, hashlib, hmac, time, os, logging
from typing import Optional
import httpx

logger = logging.getLogger("binance")

# ─── Endpoints ────────────────────────────────────────────────────────────────
DEMO_REST = "https://demo-fapi.binance.com"   # Demo Futures REST
LIVE_REST = "https://fapi.binance.com"        # Canlı Futures (klines için)
DEMO_WS   = "wss://fstream.binancefuture.com" # Demo WS stream

DEMO_KEY    = os.environ.get("BINANCE_DEMO_KEY", "")
DEMO_SECRET = os.environ.get("BINANCE_DEMO_SECRET", "")
USE_DEMO    = bool(DEMO_KEY and DEMO_SECRET)

# ─── İmza ─────────────────────────────────────────────────────────────────────
def _sign(query_string: str, secret: str) -> str:
    return hmac.new(secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()

def _build_query(params: dict) -> str:
    return "&".join(f"{k}={v}" for k, v in params.items())

# ─── Public: Klines ───────────────────────────────────────────────────────────

async def fetch_klines(
    symbol: str = "ETHUSDT",
    interval: str = "15m",
    limit: int = 500,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[dict]:
    """Futures klines — public, key gerektirmez. demo-fapi kullan."""
    params: dict = {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)}
    if start_ms: params["startTime"] = start_ms
    if end_ms:   params["endTime"]   = end_ms

    # Klines: ÖNCE canlı fapi (gerçek zamanlı), demo-fapi fallback (1dk gecikmeli)
    for base in [LIVE_REST, DEMO_REST]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(f"{base}/fapi/v1/klines", params=params)
                if r.status_code == 200:
                    raw = r.json()
                    now_ms = int(time.time() * 1000)
                    return [{
                        "ts":       int(x[0]),
                        "open":     float(x[1]),
                        "high":     float(x[2]),
                        "low":      float(x[3]),
                        "close":    float(x[4]),
                        "volume":   float(x[5]),
                        "close_ts": int(x[6]),
                        "closed":   now_ms >= int(x[6]),
                    } for x in raw]
        except Exception as e:
            logger.warning(f"klines {base} hatası: {e}")
            continue
    return []


async def fetch_ticker(symbol: str = "ETHUSDT") -> dict:
    """24hr ticker."""
    for base in [LIVE_REST, DEMO_REST]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{base}/fapi/v1/ticker/24hr", params={"symbol": symbol})
                if r.status_code == 200:
                    d = r.json()
                    return {
                        "price":      float(d["lastPrice"]),
                        "change_pct": float(d["priceChangePercent"]),
                        "high_24h":   float(d["highPrice"]),
                        "low_24h":    float(d["lowPrice"]),
                        "volume_24h": float(d["volume"]),
                        "ts":         int(time.time() * 1000),
                    }
        except Exception as e:
            logger.warning(f"ticker {base}: {e}")
    return {}


async def fetch_mark_price(symbol: str = "ETHUSDT") -> dict:
    """Mark price + funding rate."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(f"{LIVE_REST}/fapi/v1/premiumIndex", params={"symbol": symbol})
            if r.status_code == 200:
                d = r.json()
                return {
                    "mark_price":    float(d.get("markPrice", 0)),
                    "index_price":   float(d.get("indexPrice", 0)),
                    "funding_rate":  float(d.get("lastFundingRate", 0)),
                    "next_funding":  int(d.get("nextFundingTime", 0)),
                }
    except Exception as e:
        logger.warning(f"mark_price: {e}")
    return {}


# ─── Demo Trade Client ─────────────────────────────────────────────────────────

class BinanceDemoFutures:
    """
    demo-fapi.binance.com üzerinden işlem.
    Key yoksa Paper Mode (local simülasyon).
    """

    def __init__(self):
        self.key    = DEMO_KEY
        self.secret = DEMO_SECRET
        self.active = USE_DEMO
        self.base   = DEMO_REST
        mode = "DEMO FUTURES (demo-fapi.binance.com)" if self.active else "PAPER (local)"
        logger.info(f"Binance mode: {mode}")

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.key, "Content-Type": "application/x-www-form-urlencoded"}

    def _signed(self, params: dict) -> tuple[str, str]:
        """(query_string, signature) döndürür."""
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = 5000
        qs = _build_query(params)
        sig = _sign(qs, self.secret)
        return qs, sig

    async def _get(self, path: str, params: dict = None) -> dict:
        params = params or {}
        qs, sig = self._signed(params)
        url = f"{self.base}{path}?{qs}&signature={sig}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(url, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, params: dict = None) -> dict:
        params = params or {}
        qs, sig = self._signed(params)
        body = f"{qs}&signature={sig}"
        url = f"{self.base}{path}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(url, content=body, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def _delete(self, path: str, params: dict = None) -> dict:
        params = params or {}
        qs, sig = self._signed(params)
        url = f"{self.base}{path}?{qs}&signature={sig}"
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.delete(url, headers=self._headers())
            r.raise_for_status()
            return r.json()

    # ── Hesap ────────────────────────────────────────────────────────────────

    async def get_account(self) -> dict:
        if not self.active:
            return {
                "totalWalletBalance": "10000.0",
                "totalUnrealizedProfit": "0.0",
                "totalMarginBalance": "10000.0",
                "availableBalance": "10000.0",
                "assets": [{"asset": "USDT", "walletBalance": "10000.0", "unrealizedProfit": "0.0"}],
                "positions": [],
                "mode": "paper",
            }
        return await self._get("/fapi/v2/account")

    async def get_balance(self) -> list:
        if not self.active:
            return [{"asset": "USDT", "balance": "10000.0", "availableBalance": "10000.0"}]
        return await self._get("/fapi/v2/balance")

    async def get_position_risk(self, symbol: str = None) -> list:
        if not self.active:
            return []
        params = {}
        if symbol: params["symbol"] = symbol
        return await self._get("/fapi/v2/positionRisk", params)

    # ── Kaldıraç ─────────────────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        if not self.active:
            return {"leverage": leverage, "symbol": symbol, "mode": "paper"}
        return await self._post("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        """margin_type: ISOLATED | CROSSED"""
        if not self.active:
            return {"mode": "paper"}
        try:
            return await self._post("/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})
        except httpx.HTTPStatusError as e:
            # -4046: zaten bu modda → ignore
            if b"-4046" in e.response.content:
                return {"already": margin_type}
            raise

    # ── Order ─────────────────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: str,             # BUY | SELL
        quantity: float,
        order_type: str = "MARKET",
        price: float = None,
        stop_price: float = None,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        position_side: str = "BOTH",   # BOTH | LONG | SHORT
    ) -> dict:
        if not self.active:
            return {
                "orderId":      int(time.time() * 1000),
                "symbol":       symbol,
                "side":         side,
                "type":         order_type,
                "origQty":      str(quantity),
                "executedQty":  str(quantity),
                "avgPrice":     str(price or 0),
                "status":       "FILLED",
                "positionSide": position_side,
                "mode":         "paper",
                "updateTime":   int(time.time() * 1000),
            }

        params: dict = {
            "symbol":       symbol,
            "side":         side,
            "type":         order_type,
            "quantity":     f"{quantity:.6f}",
            "positionSide": position_side,
        }
        if order_type == "LIMIT":
            params["price"]       = f"{price:.2f}"
            params["timeInForce"] = time_in_force
        if stop_price:
            params["stopPrice"] = f"{stop_price:.2f}"
        if reduce_only:
            params["reduceOnly"] = "true"

        return await self._post("/fapi/v1/order", params)

    async def place_market(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
        return await self.place_order(symbol, side, quantity, "MARKET", reduce_only=reduce_only)

    async def place_stop_market(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """Stop-market order (SL için)."""
        return await self.place_order(
            symbol, side, quantity, "STOP_MARKET",
            stop_price=stop_price, reduce_only=True
        )

    async def place_take_profit_market(self, symbol: str, side: str, quantity: float, stop_price: float) -> dict:
        """Take-profit market order."""
        return await self.place_order(
            symbol, side, quantity, "TAKE_PROFIT_MARKET",
            stop_price=stop_price, reduce_only=True
        )

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        if not self.active:
            return {"orderId": order_id, "status": "CANCELED", "mode": "paper"}
        return await self._delete("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    async def cancel_all_orders(self, symbol: str) -> dict:
        if not self.active:
            return {"mode": "paper"}
        return await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def get_open_orders(self, symbol: str = None) -> list:
        if not self.active:
            return []
        params = {}
        if symbol: params["symbol"] = symbol
        return await self._get("/fapi/v1/openOrders", params)

    async def get_order(self, symbol: str, order_id: int) -> dict:
        if not self.active:
            return {"orderId": order_id, "status": "FILLED"}
        return await self._get("/fapi/v1/order", {"symbol": symbol, "orderId": order_id})


# Singleton
demo = BinanceDemoFutures()
