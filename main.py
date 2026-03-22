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
import websockets

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

# ─── Stream ────────────────────────────────────────────────────────────────────
_last_closed_ts: int = 0
_ws_connected:   bool = False   # WS bağlı mı? REST loop bunu kontrol eder

BNWS = "wss://fstream.binancefuture.com/stream?streams={s}@kline_{iv}/{s}@ticker/{s}@markPrice@1s"


async def _init_history():
    """Başlangıçta REST'ten 500 mum çek, son kapanan mumu sinyal motoruna gönder."""
    global _last_closed_ts
    try:
        logger.info("Geçmiş yükleniyor...")
        candles = await fetch_klines(SYMBOL, INTERVAL, limit=500)
        if candles:
            closed = [c for c in candles if c["closed"]]
            if closed:
                state.load_history(closed)
                _last_closed_ts = closed[-1]["ts"]
                logger.info(f"{len(closed)} mum yüklendi, son={closed[-1]['close']:.2f}")
                # Son kapanan mumu tekrar push et → sinyal motoru başlangıçta çalışsın
                state.push_candle(closed[-1], is_closed=True)
                logger.info(f"[INIT] Sinyal motoru başlangıç push: C={closed[-1]['close']:.2f}")
    except Exception as e:
        logger.error(f"Init hatası: {e}")


def _parse_kline(c: dict) -> dict:
    """Kline dict'ini normalize et, closed flag ekle."""
    now_ms = int(time.time() * 1000)
    c["closed"] = c.get("closed", now_ms >= c.get("close_ts", 0))
    return c


async def _ws_loop():
    """
    Binance WS combined stream.
    Başarılı olduğunda _ws_connected=True, kopunca False.
    """
    global _last_closed_ts, _ws_connected
    sym = SYMBOL.lower()
    url = BNWS.format(s=sym, iv=INTERVAL)

    try:
        logger.info(f"Binance WS bağlanıyor → {url[:60]}...")
        async with websockets.connect(url, ping_interval=20, ping_timeout=15,
                                      max_size=2**20, open_timeout=8) as ws:
            _ws_connected = True
            logger.info("✓ Binance WS bağlandı — canlı stream aktif")

            async for raw in ws:
                try:
                    msg    = json.loads(raw)
                    stream = msg.get("stream", "")
                    data   = msg.get("data", {})

                    if "kline" in stream:
                        k = data.get("k", {})
                        if not k:
                            continue
                        c = {
                            "ts": int(k["t"]), "open": float(k["o"]),
                            "high": float(k["h"]), "low": float(k["l"]),
                            "close": float(k["c"]), "volume": float(k["v"]),
                            "close_ts": int(k["T"]), "closed": bool(k["x"]),
                        }
                        if c["closed"] and c["ts"] > _last_closed_ts:
                            _last_closed_ts = c["ts"]
                            state.push_candle(c, is_closed=True)
                            logger.info(f"[WS] ✓ KLINE CLOSED C={c['close']:.2f}")
                        else:
                            state.push_candle(c, is_closed=False)

                    elif "ticker" in stream:
                        await ws_manager.broadcast({"event": "ticker",
                            "price": float(data.get("c", 0)),
                            "change_pct": float(data.get("P", 0)),
                            "high_24h": float(data.get("h", 0)),
                            "low_24h": float(data.get("l", 0)),
                            "volume_24h": float(data.get("v", 0)),
                            "ts": int(time.time() * 1000),
                        })
                    elif "markPrice" in stream:
                        await ws_manager.broadcast({"event": "mark",
                            "mark_price": float(data.get("p", 0)),
                            "index_price": float(data.get("i", 0)),
                            "funding_rate": float(data.get("r", 0)),
                            "next_funding": int(data.get("T", 0)),
                        })
                except Exception as parse_err:
                    logger.debug(f"[WS] msg parse: {parse_err}")

    except Exception as e:
        logger.warning(f"[WS] Bağlantı hatası: {type(e).__name__}: {str(e)[:80]}")
    finally:
        _ws_connected = False


async def _rest_loop():
    """
    REST hızlı poll — her zaman çalışır, WS bağlıysa sadece yedek.
    WS bağlı değilse birincil veri kaynağı olur (3sn).
    WS bağlıysa pasif (60sn) — gereksiz yük yaratmaz.
    """
    global _last_closed_ts
    tick_n = 0

    while True:
        # Her zaman 3sn'de bir çalış — browser WS mum kapanışını zaten gönderir
        # REST loop: kapanmamış son mumu sürekli günceller + sinyal motorunu besler
        await asyncio.sleep(3)

        try:
            candles = await fetch_klines(SYMBOL, INTERVAL, limit=2)
            if not candles:
                logger.warning("[REST] Boş klines yanıtı")
                continue

            now_ms = int(time.time() * 1000)
            for c in candles:
                c["closed"] = now_ms >= c["close_ts"]

            # Kapalı mumları işle (sadece YENİ kapananlar)
            for c in candles:
                if c["closed"] and c["ts"] > _last_closed_ts:
                    _last_closed_ts = c["ts"]
                    state.push_candle(c, is_closed=True)
                    logger.info(f"[REST] CLOSED C={c['close']:.2f}")

            # Son açık mumu güncelle (grafik için tick)
            last = candles[-1]
            if not last["closed"]:
                state.push_candle(last, is_closed=False)

        except Exception as e:
            logger.warning(f"[REST] klines hatası: {e}")

        # Ticker + mark price (her 5 döngüde = 15sn)
        tick_n += 1
        if tick_n % 5 == 0:
            try:
                t = await fetch_ticker(SYMBOL)
                if t:
                    await ws_manager.broadcast({"event": "ticker", **t})
            except Exception:
                pass
            try:
                m = await fetch_mark_price(SYMBOL)
                if m:
                    await ws_manager.broadcast({"event": "mark", **m})
            except Exception:
                pass


async def _tv_ws_loop():
    """
    TradingView WebSocket alternatif stream.
    wss://data.tradingview.com/socket.io/websocket
    Protokol: ~m~LEN~m~JSON mesajları, unauthorized_user_token ile çalışır.
    """
    global _last_closed_ts, _ws_connected
    import re as _re

    def _tv_encode(func, args):
        msg = json.dumps({'m': func, 'p': args}, separators=(',', ''))
        return f'~m~{len(msg)}~m~{msg}'

    def _tv_decode(raw):
        parts = _re.split(r'~m~\d+~m~', raw)
        msgs = []
        for p in parts:
            p = p.strip()
            if p:
                try: msgs.append(json.loads(p))
                except: pass
        return msgs

    import random, string as _string
    def _rid(n=10): return ''.join(random.choices(_string.ascii_lowercase+_string.digits, k=n))

    url = 'wss://data.tradingview.com/socket.io/websocket'
    cs = 'cs_' + _rid()
    sym = 'BINANCE:ETHUSDT.P'
    headers = [
        ('Origin', 'https://data.tradingview.com'),
        ('User-Agent', 'Mozilla/5.0 (compatible; TradingBot/1.0)'),
    ]
    try:
        logger.info('[TV] TradingView WS bağlanıyor...')
        async with websockets.connect(url, additional_headers=headers,
                                      open_timeout=10, ping_interval=None) as ws:
            _ws_connected = True
            logger.info('[TV] TradingView WS bağlandı ✓')

            # Protokol handshake
            await ws.send(_tv_encode('set_auth_token', ['unauthorized_user_token']))
            await ws.send(_tv_encode('chart_create_session', [cs, '']))
            resolve_str = json.dumps({'symbol': sym, 'adjustment': 'splits'})
            await ws.send(_tv_encode('resolve_symbol', [cs, 'sds1', '='+resolve_str]))
            await ws.send(_tv_encode('create_series', [cs, 's1', 's1', 'sds1', '15', 500]))

            async for raw in ws:
                # Keepalive ping
                if raw.startswith('~h~'):
                    await ws.send(raw)
                    continue
                for m in _tv_decode(raw):
                    mtype = m.get('m', '')
                    p = m.get('p', [])

                    # timescale_update veya du → kline verisi
                    if mtype in ('timescale_update', 'du') and len(p) >= 2 and isinstance(p[1], dict):
                        series = p[1].get('s1', {})
                        bars = series.get('s', [])
                        ns = series.get('ns', {})
                        # ns.d → yeni gelen barlar (incremental)
                        new_bars = ns.get('d', '') if isinstance(ns, dict) else ''
                        
                        all_bars = bars if bars else []
                        for b in all_bars:
                            v = b.get('v', [])
                            if len(v) < 5: continue
                            now_ms = int(time.time() * 1000)
                            ts_ms  = int(v[0]) * 1000
                            # 15dk = 900000ms
                            close_ts_ms = ts_ms + 900000
                            is_closed   = now_ms >= close_ts_ms
                            c = {
                                'ts': ts_ms, 'open': v[1], 'high': v[2],
                                'low': v[3], 'close': v[4], 'volume': v[5] if len(v)>5 else 0,
                                'close_ts': close_ts_ms, 'closed': is_closed,
                            }
                            if is_closed and c['ts'] > _last_closed_ts:
                                _last_closed_ts = c['ts']
                                state.push_candle(c, is_closed=True)
                                logger.info(f"[TV] KLINE CLOSED C={c['close']:.2f}")
                            elif not is_closed and (not _last_closed_ts or c['ts'] >= _last_closed_ts):
                                state.push_candle(c, is_closed=False)

                    # Canlı güncelleme
                    elif mtype == 'series_completed':
                        logger.debug('[TV] series_completed')

    except Exception as e:
        logger.warning(f'[TV] WS hatası: {type(e).__name__}: {str(e)[:80]}')
    finally:
        _ws_connected = False


async def _ws_manager_loop():
    """
    WS bağlantısını yönet: önce Binance, başarısız olursa TradingView.
    _rest_loop ile paralel çalışır — birbirini bloklamaz.
    """
    retry = 0
    while True:
        # Önce Binance Futures WS dene
        await _ws_loop()

        if not _ws_connected:
            retry = min(retry + 1, 4)
            delay = 2 ** retry
            logger.info(f'[WS] Binance bağlanamadı, {delay}sn sonra TradingView deneniyor')
            await asyncio.sleep(delay)

            # TradingView WS dene
            await _tv_ws_loop()

            if not _ws_connected:
                logger.info('[WS] Her iki WS başarısız, REST aktif')
                await asyncio.sleep(30)
        else:
            retry = 0


@app.on_event("startup")
async def startup():
    await _init_history()
    asyncio.create_task(_ws_manager_loop()) # ← WS bağlantı yöneticisi
    asyncio.create_task(_rest_loop())        # ← REST yedek (WS yoksa 3sn, varsa 60sn)

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
                elif cmd == "candle_from_browser":
                    c = data.get("candle")
                    if c:
                        state.push_candle(c, is_closed=bool(c.get("closed", False)))
                elif cmd == "set_interval":
                    # Kullanıcı TF değiştirdi → yeni interval ile geçmişi yükle
                    new_iv = data.get("interval", "15m")
                    valid = ["1m","3m","5m","15m","30m","1h","2h","4h","6h","1d"]
                    if new_iv in valid:
                        global INTERVAL, _last_closed_ts
                        INTERVAL = new_iv
                        _last_closed_ts = 0
                        state.reset_history()
                        logger.info(f"[TF] Interval değişti: {new_iv}")
                        asyncio.create_task(_init_history())
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
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, ws="websockets", ws_ping_interval=20, ws_ping_timeout=30)
