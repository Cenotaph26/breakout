"""
binance_client.py — Binance Testnet + Public API İstemcisi
===========================================================
Klines: api.binance.com (public, key gerektirmez)
Demo Trade: testnet.binance.vision (TESTNET_API_KEY + TESTNET_SECRET gerekir)

Ortam değişkenleri:
  BINANCE_TESTNET_KEY    → Testnet API key
  BINANCE_TESTNET_SECRET → Testnet secret key
  USE_TESTNET            → '1' ise testnet, aksi hâlde paper mode
"""

import asyncio
import hashlib
import hmac
import time
import os
import logging
from typing import Optional
import httpx

logger = logging.getLogger("binance")

# ─── Endpoints ────────────────────────────────────────────────────────────────
PUBLIC_BASE   = "https://api.binance.com"
TESTNET_BASE  = "https://testnet.binance.vision"

TESTNET_KEY    = os.environ.get("BINANCE_TESTNET_KEY", "")
TESTNET_SECRET = os.environ.get("BINANCE_TESTNET_SECRET", "")
USE_TESTNET    = os.environ.get("USE_TESTNET", "0") == "1"


# ─── İmza ─────────────────────────────────────────────────────────────────────
def _sign(params: dict, secret: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


# ─── Public: Kline/Candlestick veri çekme ─────────────────────────────────────

async def fetch_klines(
    symbol: str = "ETHUSDT",
    interval: str = "15m",
    limit: int = 500,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> list[dict]:
    """
    Binance klines çek — public endpoint, key gerektirmez.
    Dönüş: [{ts, open, high, low, close, volume, close_ts}, ...]
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms: params["startTime"] = start_ms
    if end_ms:   params["endTime"]   = end_ms

    url = f"{PUBLIC_BASE}/api/v3/klines"

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        raw = resp.json()

    return [
        {
            "ts":       int(r[0]),
            "open":     float(r[1]),
            "high":     float(r[2]),
            "low":      float(r[3]),
            "close":    float(r[4]),
            "volume":   float(r[5]),
            "close_ts": int(r[6]),
            "closed":   True,        # geçmiş mumlar hep kapalı
        }
        for r in raw
    ]


async def fetch_history_15m(symbol: str = "ETHUSDT", months: int = 5) -> list[dict]:
    """
    Son N aylık 15dk mum geçmişini çek (max 1000 mum/istek → döngüyle).
    """
    all_candles: list[dict] = []
    end_ms = int(time.time() * 1000)
    # 5 ay ≈ 150 gün × 24 × 4 = 14400 mum → 15 istek (1000 limit)
    step_ms = 1000 * 15 * 60 * 1000  # 1000 mum × 15dk

    total_needed = months * 30 * 24 * 4  # yaklaşık mum sayısı
    fetched = 0

    while fetched < total_needed:
        start_ms = end_ms - step_ms
        batch = await fetch_klines(symbol, "15m", 1000, start_ms, end_ms)
        if not batch:
            break
        all_candles = batch + all_candles
        end_ms = batch[0]["ts"] - 1
        fetched += len(batch)
        if len(batch) < 1000:
            break  # başa ulaştık
        await asyncio.sleep(0.1)  # rate limit

    # Duplicate temizle, sırala
    seen = set()
    unique = []
    for c in sorted(all_candles, key=lambda x: x["ts"]):
        if c["ts"] not in seen:
            seen.add(c["ts"])
            unique.append(c)

    return unique


async def fetch_latest_candle(symbol: str = "ETHUSDT") -> dict:
    """Son mevcut 15dk mumu çek (henüz kapanmamış olabilir)."""
    candles = await fetch_klines(symbol, "15m", limit=2)
    if not candles:
        return {}
    latest = candles[-1]
    # Son mum henüz kapanmamışsa closed=False
    now_ms = int(time.time() * 1000)
    latest["closed"] = now_ms >= latest["close_ts"]
    return latest


async def fetch_ticker(symbol: str = "ETHUSDT") -> dict:
    """Anlık fiyat ticker."""
    url = f"{PUBLIC_BASE}/api/v3/ticker/24hr"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, params={"symbol": symbol})
        resp.raise_for_status()
        d = resp.json()
    return {
        "price":      float(d["lastPrice"]),
        "change_pct": float(d["priceChangePercent"]),
        "high_24h":   float(d["highPrice"]),
        "low_24h":    float(d["lowPrice"]),
        "volume_24h": float(d["volume"]),
        "ts":         int(time.time() * 1000),
    }


# ─── Testnet: Demo Trade ───────────────────────────────────────────────────────

class BinanceTestnet:
    """
    Binance Testnet üzerinde gerçek API çağrıları yapar.
    Key/Secret olmadığında Paper Mode (local simülasyon) çalışır.
    """

    def __init__(self):
        self.key    = TESTNET_KEY
        self.secret = TESTNET_SECRET
        self.active = bool(self.key and self.secret and USE_TESTNET)
        self.base   = TESTNET_BASE if self.active else None
        logger.info(f"Binance mode: {'TESTNET' if self.active else 'PAPER'}")

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.key}

    def _signed_params(self, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = _sign(params, self.secret)
        return params

    async def get_account(self) -> dict:
        if not self.active:
            return {"balances": [{"asset": "USDT", "free": "10000.0", "locked": "0.0"},
                                  {"asset": "ETH",  "free": "10.0",    "locked": "0.0"}],
                    "mode": "paper"}
        params = self._signed_params({})
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base}/api/v3/account",
                                  headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()

    async def place_order(
        self,
        symbol: str,
        side: str,           # BUY | SELL
        quantity: float,
        order_type: str = "MARKET",
        price: float = None,
        stop_price: float = None,
        time_in_force: str = "GTC",
    ) -> dict:
        """
        Market veya Limit order aç.
        Paper modda simüle eder, gerçek API çağrısı yapmaz.
        """
        if not self.active:
            # Paper mode simülasyonu
            return {
                "orderId":       int(time.time() * 1000),
                "symbol":        symbol,
                "side":          side,
                "type":          order_type,
                "origQty":       str(quantity),
                "executedQty":   str(quantity),
                "price":         str(price or 0),
                "status":        "FILLED",
                "mode":          "paper",
                "transactTime":  int(time.time() * 1000),
            }

        params: dict = {
            "symbol":   symbol,
            "side":     side,
            "type":     order_type,
            "quantity": f"{quantity:.6f}",
        }
        if order_type == "LIMIT":
            params["price"]       = f"{price:.2f}"
            params["timeInForce"] = time_in_force
        if stop_price:
            params["stopPrice"] = f"{stop_price:.2f}"

        params = self._signed_params(params)
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{self.base}/api/v3/order",
                                   headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()

    async def cancel_order(self, symbol: str, order_id: int) -> dict:
        if not self.active:
            return {"orderId": order_id, "status": "CANCELED", "mode": "paper"}
        params = self._signed_params({"symbol": symbol, "orderId": order_id})
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{self.base}/api/v3/order",
                                     headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()

    async def get_open_orders(self, symbol: str = "ETHUSDT") -> list:
        if not self.active:
            return []
        params = self._signed_params({"symbol": symbol})
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base}/api/v3/openOrders",
                                  headers=self._headers(), params=params)
            r.raise_for_status()
            return r.json()


# Singleton
testnet = BinanceTestnet()
