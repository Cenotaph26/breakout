"""
main.py — Ana Uygulama (Demo Futures)
======================================
• FastAPI + WebSocket
• demo-fapi.binance.com klines + demo trade
• 15sn poll döngüsü → mum kapanışında sinyal
"""

import asyncio, json, logging, os, time
from datetime import datetime
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import bot_state as state
from binance_client import fetch_klines, fetch_ticker, fetch_mark_price, demo

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("main")

app = FastAPI(title="ETH Trend Bot — Demo Futures", version="3.1")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
os.makedirs(STATIC_DIR, exist_ok=True)

SYMBOL   = os.environ.get("SYMBOL", "ETHUSDT")
INTERVAL = "15m"

# ─── WebSocket Manager ─────────────────────────────────────────────────────────
class WSManager:
    def __init__(self): self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept(); self.active.add(ws)
        logger.info(f"WS +1 → toplam {len(self.active)}")

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        logger.info(f"WS -1 → toplam {len(self.active)}")

    async def broadcast(self, data: dict):
        if not self.active: return
        msg  = json.dumps(data, ensure_ascii=False, default=str)
        dead = set()
        for ws in list(self.active):
            try: await ws.send_text(msg)
            except: dead.add(ws)
        for ws in dead: self.active.discard(ws)

ws_manager = WSManager()

async def _broadcast(data: dict):
    await ws_manager.broadcast(data)

state.set_broadcast(_broadcast)

# ─── Poller ────────────────────────────────────────────────────────────────────
_last_closed_ts: int = 0
_init_done: bool = False

async def _init_history():
    global _init_done, _last_closed_ts
    try:
        logger.info("Geçmiş yükleniyor (demo-fapi)...")
        candles = await fetch_klines(SYMBOL, INTERVAL, limit=500)
        if candles:
            closed = [c for c in candles if c["closed"]]
            if closed:
                state.load_history(closed)
                _last_closed_ts = closed[-1]["ts"]
                logger.info(f"{len(closed)} mum yüklendi, son={closed[-1]['close']:.2f}")
    except Exception as e:
        logger.error(f"Init hatası: {e}")
    finally:
        _init_done = True

async def _poll_loop():
    global _last_closed_ts
    while True:
        try:
            candles = await fetch_klines(SYMBOL, INTERVAL, limit=3)
            now_ms  = int(time.time() * 1000)
            for c in candles:
                c["closed"] = now_ms >= c["close_ts"]
                if c["closed"] and c["ts"] > _last_closed_ts:
                    _last_closed_ts = c["ts"]
                    state.push_candle(c, is_closed=True)
                elif not c["closed"] and candles and c["ts"] == candles[-1]["ts"]:
                    state.push_candle(c, is_closed=False)

            # Ticker + mark price
            try:
                ticker = await fetch_ticker(SYMBOL)
                if ticker: await ws_manager.broadcast({"event": "ticker", **ticker})
            except: pass
            try:
                mark = await fetch_mark_price(SYMBOL)
                if mark: await ws_manager.broadcast({"event": "mark", **mark})
            except: pass

        except Exception as e:
            logger.error(f"Poll: {e}")
        await asyncio.sleep(15)

@app.on_event("startup")
async def startup():
    await _init_history()
    asyncio.create_task(_poll_loop())

# ─── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        await ws.send_text(json.dumps({"event": "full_state", **state.get_state()}, default=str))
        while True:
            try:
                msg  = await asyncio.wait_for(ws.receive_text(), timeout=25.0)
                data = json.loads(msg)
                cmd  = data.get("cmd", "")

                if cmd == "ping":
                    await ws.send_text(json.dumps({"event":"pong","ts":int(time.time()*1000)}))
                elif cmd == "get_state":
                    await ws.send_text(json.dumps({"event":"full_state",**state.get_state()}, default=str))
                elif cmd == "start":   state.start_bot()
                elif cmd == "stop":    state.stop_bot()
                elif cmd == "pause":   state.pause_bot()
                elif cmd == "close_position":
                    res = state.close_position_manual("manual")
                    # Demo'da gerçek order kapat
                    if demo.active and state._position is None:
                        try:
                            opp = "SELL" if data.get("direction","long")=="long" else "BUY"
                            qty = float(data.get("qty", 0.01))
                            await demo.place_market(SYMBOL, opp, qty, reduce_only=True)
                        except Exception as e:
                            logger.warning(f"Demo close order: {e}")
                    await ws.send_text(json.dumps({"event":"position_closed",**res}))
                elif cmd == "update_config":
                    state.update_config(regime=data.get("regime"), params=data.get("params"))
                elif cmd == "manual_order":
                    res = await _place_demo_order(data)
                    await ws.send_text(json.dumps({"event":"order_result",**res}))

            except asyncio.TimeoutError:
                try: await ws.send_text(json.dumps({"event":"keepalive"}))
                except: break
    except WebSocketDisconnect: pass
    finally: ws_manager.disconnect(ws)

# ─── Demo Order Yardımcısı ─────────────────────────────────────────────────────
async def _place_demo_order(data: dict) -> dict:
    try:
        side     = data.get("side", "BUY")
        qty      = float(data.get("qty", 0.01))
        otype    = data.get("type", "MARKET")
        price    = data.get("price")
        leverage = int(data.get("leverage", 5))

        if demo.active:
            await demo.set_leverage(SYMBOL, leverage)
        result = await demo.place_order(
            SYMBOL, side, qty, order_type=otype,
            price=float(price) if price else None,
        )
        return {"ok": True, "order": result}
    except Exception as e:
        logger.error(f"Demo order: {e}")
        return {"ok": False, "error": str(e)}

# ─── REST ──────────────────────────────────────────────────────────────────────
@app.get("/api/state")
def api_state(): return state.get_state()

@app.get("/api/candles")
async def api_candles(limit: int = Query(300, le=1500)):
    return await fetch_klines(SYMBOL, INTERVAL, limit=limit)

@app.get("/api/ticker")
async def api_ticker():
    try: return await fetch_ticker(SYMBOL)
    except Exception as e: return {"error": str(e)}

@app.get("/api/mark")
async def api_mark():
    try: return await fetch_mark_price(SYMBOL)
    except Exception as e: return {"error": str(e)}

@app.get("/api/account")
async def api_account():
    try: return await demo.get_account()
    except Exception as e: return {"error": str(e)}

@app.get("/api/positions")
async def api_positions():
    try: return await demo.get_position_risk(SYMBOL)
    except Exception as e: return {"error": str(e)}

@app.get("/api/orders")
async def api_orders():
    try: return await demo.get_open_orders(SYMBOL)
    except Exception as e: return {"error": str(e)}

@app.post("/api/bot/start")
def bot_start(): state.start_bot(); return {"ok":True}

@app.post("/api/bot/stop")
def bot_stop():  state.stop_bot();  return {"ok":True}

@app.post("/api/bot/pause")
def bot_pause(): state.pause_bot(); return {"ok":True}

@app.post("/api/position/close")
def pos_close(): return state.close_position_manual("manual_api")

@app.post("/api/config")
async def api_config(body: dict = Body(...)):
    state.update_config(regime=body.get("regime"), params=body.get("params"))
    return {"ok":True}

@app.post("/api/order")
async def api_order(body: dict = Body(...)):
    return await _place_demo_order(body)

@app.post("/api/leverage")
async def api_leverage(body: dict = Body(...)):
    try:
        res = await demo.set_leverage(SYMBOL, int(body.get("leverage",5)))
        return {"ok":True, **res}
    except Exception as e: return {"ok":False,"error":str(e)}

@app.post("/api/cancel_all")
async def api_cancel_all():
    try:
        res = await demo.cancel_all_orders(SYMBOL)
        return {"ok":True, **res}
    except Exception as e: return {"ok":False,"error":str(e)}

@app.get("/export/trades")
def export_trades():
    csv_data = state.get_trades_csv()
    if not csv_data: return JSONResponse({"error":"İşlem yok"})
    return StreamingResponse(iter([csv_data]), media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=trades.csv"})

@app.get("/export/logs")
def export_logs():
    lines = "\n".join(f"[{l['time']}] {l['level']}: {l['msg']}" for l in state.get_logs())
    return StreamingResponse(iter([lines]), media_type="text/plain",
        headers={"Content-Disposition":"attachment; filename=bot_logs.txt"})

@app.get("/export/equity")
def export_equity():
    import io, csv as cmod
    data = state._equity_curve
    if not data: return JSONResponse({"error":"Veri yok"})
    buf = io.StringIO()
    w = cmod.DictWriter(buf, fieldnames=["ts","equity"])
    w.writeheader(); w.writerows(data)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=equity.csv"})

@app.get("/health")
def health():
    return {"status":"ok","symbol":SYMBOL,"candles":len(state._candles),
            "regime":state._regime,"running":state._bot_running,
            "demo_active":demo.active,"ws_clients":len(ws_manager.active),
            "ts":datetime.now().isoformat()}

@app.get("/")
def index(): return FileResponse(os.path.join(STATIC_DIR,"index.html"))

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
