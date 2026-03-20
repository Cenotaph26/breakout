"""
main.py — Ana Uygulama
=======================
• FastAPI + WebSocket
• 15dk mum döngüsü (her 30sn poll → kapanınca tam işlem)
• REST + WS endpoints
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

import bot_state as state
from binance_client import fetch_klines, fetch_latest_candle, fetch_ticker, testnet

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="ETH Trend Bot", version="3.0")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

SYMBOL   = os.environ.get("SYMBOL", "ETHUSDT")
INTERVAL = "15m"

# ─── WebSocket Manager ────────────────────────────────────────────────────────

class WSManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)
        logger.info(f"WS bağlandı. Toplam: {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        logger.info(f"WS ayrıldı. Toplam: {len(self.active)}")

    async def broadcast(self, data: dict):
        if not self.active:
            return
        msg = json.dumps(data, ensure_ascii=False)
        dead = set()
        for ws in self.active:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)


ws_manager = WSManager()

async def _broadcast(data: dict):
    await ws_manager.broadcast(data)

state.set_broadcast(_broadcast)


# ─── Binance Poller ───────────────────────────────────────────────────────────

_last_closed_ts: int = 0
_init_done: bool = False


async def _init_history():
    """Başlangıçta son 500 mumu çek."""
    global _init_done
    try:
        logger.info("Geçmiş veriler çekiliyor...")
        candles = await fetch_klines(SYMBOL, INTERVAL, limit=500)
        if candles:
            # Son mum henüz kapanmamış olabilir — onu çıkar
            closed = [c for c in candles if c["closed"]]
            state.load_history(closed)
            global _last_closed_ts
            _last_closed_ts = closed[-1]["ts"] if closed else 0
            logger.info(f"{len(closed)} mum yüklendi")
        _init_done = True
    except Exception as e:
        logger.error(f"Geçmiş yükleme hatası: {e}")
        _init_done = True  # yine de devam et


async def _poll_loop():
    """Her 15 saniyede bir Binance'tan son 2 mumu çek."""
    global _last_closed_ts

    while True:
        try:
            candles = await fetch_klines(SYMBOL, INTERVAL, limit=3)
            if not candles:
                await asyncio.sleep(15)
                continue

            now_ms = int(time.time() * 1000)

            for c in candles:
                # Kapanmış mu?
                c["closed"] = now_ms >= c["close_ts"]

                if c["closed"] and c["ts"] > _last_closed_ts:
                    # Yeni kapanmış mum
                    _last_closed_ts = c["ts"]
                    state.push_candle(c, is_closed=True)
                    logger.info(f"Yeni 15dk mum: {c['ts']}  close={c['close']}")
                elif not c["closed"] and c["ts"] >= candles[-1]["ts"]:
                    # Canlı (kapanmamış) son mum
                    state.push_candle(c, is_closed=False)

            # Ticker da güncelle
            try:
                ticker = await fetch_ticker(SYMBOL)
                await ws_manager.broadcast({"event": "ticker", **ticker})
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Poll hatası: {e}")

        await asyncio.sleep(15)  # 15 saniyede bir poll


@app.on_event("startup")
async def startup():
    await _init_history()
    asyncio.create_task(_poll_loop())
    logger.info("Bot başlatıldı")


# ─── WebSocket ────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        # İlk bağlantıda tüm state'i gönder
        full = state.get_state()
        await ws.send_text(json.dumps({"event": "full_state", **full}))

        # Client'tan gelen mesajları dinle
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                data = json.loads(msg)
                cmd = data.get("cmd", "")

                if cmd == "ping":
                    await ws.send_text(json.dumps({"event": "pong", "ts": int(time.time()*1000)}))
                elif cmd == "get_state":
                    full = state.get_state()
                    await ws.send_text(json.dumps({"event": "full_state", **full}))
                elif cmd == "start":
                    state.start_bot()
                elif cmd == "stop":
                    state.stop_bot()
                elif cmd == "pause":
                    state.pause_bot()
                elif cmd == "close_position":
                    result = state.close_position_manual("manual")
                    await ws.send_text(json.dumps({"event": "position_closed", **result}))
                elif cmd == "update_config":
                    state.update_config(
                        regime=data.get("regime"),
                        params=data.get("params"),
                    )

            except asyncio.TimeoutError:
                # Keepalive ping
                try:
                    await ws.send_text(json.dumps({"event": "keepalive"}))
                except Exception:
                    break

    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(ws)


# ─── REST Endpoints ───────────────────────────────────────────────────────────

@app.get("/api/state")
def api_state():
    return state.get_state()

@app.get("/api/candles")
async def api_candles(limit: int = Query(300, le=500)):
    candles = await fetch_klines(SYMBOL, INTERVAL, limit=limit)
    return candles

@app.get("/api/ticker")
async def api_ticker():
    try:
        return await fetch_ticker(SYMBOL)
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/bot/start")
def bot_start():
    state.start_bot(); return {"ok": True}

@app.post("/api/bot/stop")
def bot_stop():
    state.stop_bot(); return {"ok": True}

@app.post("/api/bot/pause")
def bot_pause():
    state.pause_bot(); return {"ok": True}

@app.post("/api/position/close")
def position_close():
    return state.close_position_manual("manual_api")

@app.post("/api/config")
async def update_config(body: dict):
    state.update_config(regime=body.get("regime"), params=body.get("params"))
    return {"ok": True}

@app.get("/api/account")
async def get_account():
    try:
        return await testnet.get_account()
    except Exception as e:
        return {"error": str(e)}

@app.get("/export/trades")
def export_trades():
    csv_data = state.get_trades_csv()
    if not csv_data:
        return JSONResponse({"error": "İşlem yok"})
    return StreamingResponse(
        iter([csv_data]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trades.csv"}
    )

@app.get("/export/logs")
def export_logs():
    logs = state.get_logs()
    lines = "\n".join(f"[{l['time']}] {l['level']}: {l['msg']}" for l in logs)
    return StreamingResponse(
        iter([lines]), media_type="text/plain",
        headers={"Content-Disposition": "attachment; filename=bot_logs.txt"}
    )

@app.get("/export/equity")
def export_equity():
    import io, csv as cmod
    data = state._equity_curve
    if not data:
        return JSONResponse({"error": "Veri yok"})
    buf = io.StringIO()
    w = cmod.DictWriter(buf, fieldnames=["ts","equity"])
    w.writeheader(); w.writerows(data)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=equity.csv"}
    )

@app.get("/health")
def health():
    return {
        "status": "ok",
        "candles": len(state._candles),
        "regime": state._regime,
        "bot_running": state._bot_running,
        "ws_clients": len(ws_manager.active),
        "ts": datetime.now().isoformat(),
    }

@app.get("/")
def index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
