"""
bot_state.py — Canlı Bot Durum Yöneticisi
==========================================
• 15dk mumları bellekte tutar (son 500 mum)
• Her mum kapanışında sinyal üretir
• Pozisyonu takip eder
• Log + equity kaydeder
• WebSocket broadcast için event sistemi
"""

import asyncio
import time
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable
from datetime import datetime

from engine import (Candle, TrendLine, detect_regime, RegimeConfig,
                    get_best_lines, is_range,
                    calc_dynamic_stop, calc_tp1, calc_atr, is_pivot_high,
                    is_pivot_low, _pnl, COMMISSION,
                    PIVOT_N as _PIVOT_N, MIN_TOUCH as _MIN_TOUCH,
                    CONFIRM as _CONFIRM, VOL_MULT as _VOL_MULT,
                    MOM_K as _MOM_K, MOM_MIN as _MOM_MIN,
                    BREAK_MARGIN as _BREAK_MARGIN,
                    vol_avg, market_direction)

logger = logging.getLogger("bot")

# ─── Sabitler ─────────────────────────────────────────────────────────────────
SYMBOL       = os.environ.get("SYMBOL", "ETHUSDT")
MAX_CANDLES  = 600      # bellekte tutulacak max 15dk mum
LOG_MAX      = 500      # log satırı limiti
REGIME_EVERY = 48       # kaç mumda bir rejim güncellenir (48 × 15dk = 12 saat)
PIVOT_N      = 2
LINE_REFRESH = 3        # daha sık çizgi güncelleme (3 × 15dk = 45dk)


# ─── Veri yapıları ────────────────────────────────────────────────────────────

@dataclass
class LivePosition:
    direction:    str
    entry_price:  float
    stop:         float
    tp1:          float
    size:         float = 1.0
    half_closed:  bool  = False
    best_price:   float = 0.0
    entry_ts:     int   = 0
    entry_time:   str   = ""
    order_id:     Optional[int] = None
    pnl_pct:      float = 0.0   # anlık PnL

@dataclass
class ClosedTrade:
    direction:   str
    entry_price: float
    exit_price:  float
    entry_time:  str
    exit_time:   str
    exit_reason: str
    pnl_pct:     float
    size:        float
    regime:      str

@dataclass
class BotStats:
    total_trades:     int   = 0
    wins:             int   = 0
    losses:           int   = 0
    total_pnl:        float = 0.0
    equity:           float = 1.0
    max_drawdown:     float = 0.0
    peak_equity:      float = 1.0
    running_since:    str   = ""
    last_signal_time: str   = ""

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades * 100 if self.total_trades else 0

    @property
    def profit_factor(self) -> float:
        wins_sum  = sum(t.pnl_pct * t.size for t in _closed_trades if t.pnl_pct > 0)
        loss_sum  = abs(sum(t.pnl_pct * t.size for t in _closed_trades if t.pnl_pct <= 0))
        return wins_sum / loss_sum if loss_sum > 0 else 999.0


# ─── Global Bot State ─────────────────────────────────────────────────────────

_candles:      list[dict]    = []      # raw dicts from Binance API
_candle_objs:  list[Candle]  = []      # Candle objects for engine
_ph:           list[int]     = []      # pivot highs
_pl:           list[int]     = []      # pivot lows
_res:          Optional[TrendLine] = None
_sup:          Optional[TrendLine] = None
_last_line_update: int = 0
_regime:       str  = "NEUTRAL"
_regime_score: float = 0.0
_regime_counter: int = 0
_cfg:          RegimeConfig = RegimeConfig(
    trend_filter_window=15, trailing_step=0.003, tp1_min_rr=1.5,
    long_trend_req='any', short_trend_req='down', long_only=False, vol_filter_min=0.0
)  # 15dk backtest en iyisi

_position:     Optional[LivePosition] = None
_closed_trades: list[ClosedTrade]     = []
_stats:        BotStats = BotStats()
_log_lines:    list[dict] = []         # {time, level, msg}
_equity_curve: list[dict] = []         # {ts, equity}
_bot_running:  bool = False
_bot_paused:   bool = False

# WebSocket broadcast callback
_broadcast_fn: Optional[Callable] = None


def set_broadcast(fn: Callable):
    global _broadcast_fn
    _broadcast_fn = fn


def _broadcast(event: str, data: dict):
    if _broadcast_fn:
        asyncio.create_task(_broadcast_fn({"event": event, **data}))


# ─── Log ──────────────────────────────────────────────────────────────────────

def _log(msg: str, level: str = "INFO"):
    now = datetime.now().strftime("%H:%M:%S")
    entry = {"time": now, "level": level, "msg": msg}
    _log_lines.append(entry)
    if len(_log_lines) > LOG_MAX:
        _log_lines.pop(0)
    logger.info(f"[{level}] {msg}")
    _broadcast("log", entry)


# ─── Candle yönetimi ──────────────────────────────────────────────────────────

def load_history(candles_raw: list[dict]):
    """Başlangıçta geçmiş veriyi yükle."""
    global _candles, _candle_objs, _ph, _pl, _res, _sup, _last_line_update
    global _regime, _regime_score, _cfg, _regime_counter

    _candles     = candles_raw[-MAX_CANDLES:]
    _candle_objs = [_to_candle(c) for c in _candles]

    # Pivot hesapla
    n = len(_candle_objs)
    _ph = []; _pl = []
    for i in range(PIVOT_N * 2 + 2, n - PIVOT_N):
        if is_pivot_high(_candle_objs, i, PIVOT_N): _ph.append(i)
        if is_pivot_low (_candle_objs, i, PIVOT_N): _pl.append(i)

    # Trend çizgileri
    if n > 10:
        _res, _sup = get_best_lines(_ph, _pl, _candle_objs, n - 1)
    _last_line_update = n - 1

    # Rejim
    if n > 50:
        _regime, _regime_score = detect_regime(_candle_objs, n - 1)
        _cfg = RegimeConfig.for_regime(_regime)

    _stats.running_since = datetime.now().strftime("%Y-%m-%d %H:%M")
    _log(f"Geçmiş yüklendi: {n} mum, rejim={_regime}({_regime_score:.1f})")
    _broadcast("init", {"candles": _candles[-200:], "regime": _regime,
                         "lines": _get_lines_dict()})


def push_candle(candle_raw: dict, is_closed: bool = True):
    """Yeni mum ekle. is_closed=False → güncelleme (canlı mum)."""
    global _candles, _candle_objs, _ph, _pl, _res, _sup
    global _last_line_update, _regime, _regime_score, _cfg, _regime_counter

    if not is_closed:
        # Sadece son mumu güncelle (kapanmamış)
        if _candles and _candles[-1]["ts"] == candle_raw["ts"]:
            _candles[-1] = candle_raw
            _candle_objs[-1] = _to_candle(candle_raw)
        else:
            _candles.append(candle_raw)
            _candle_objs.append(_to_candle(candle_raw))
            if len(_candles) > MAX_CANDLES:
                _candles.pop(0)
                _candle_objs.pop(0)
                _ph = [p - 1 for p in _ph if p > 0]
                _pl = [p - 1 for p in _pl if p > 0]
        # Anlık PnL güncelle
        _update_live_pnl(candle_raw["close"])
        _broadcast("tick", {"candle": candle_raw, "position": _get_position_dict()})
        return

    # Kapanmış mum → tam işlem
    if _candles and _candles[-1]["ts"] == candle_raw["ts"]:
        _candles[-1] = candle_raw
        _candle_objs[-1] = _to_candle(candle_raw)
    else:
        _candles.append(candle_raw)
        _candle_objs.append(_to_candle(candle_raw))

    if len(_candles) > MAX_CANDLES:
        _candles.pop(0)
        _candle_objs.pop(0)
        _ph = [p - 1 for p in _ph if p > 0]
        _pl = [p - 1 for p in _pl if p > 0]

    n = len(_candle_objs)
    i = n - 1

    # Pivot güncelle
    ci = i - PIVOT_N
    if ci >= PIVOT_N:
        if is_pivot_high(_candle_objs, ci, PIVOT_N) and ci not in _ph: _ph.append(ci)
        if is_pivot_low (_candle_objs, ci, PIVOT_N) and ci not in _pl: _pl.append(ci)

    # Trend çizgileri
    if i - _last_line_update >= LINE_REFRESH:
        _res, _sup = get_best_lines(_ph, _pl, _candle_objs, i)
        _last_line_update = i

    # Rejim güncelle
    _regime_counter += 1
    if _regime_counter >= REGIME_EVERY and n > 100:
        new_regime, score = detect_regime(_candle_objs, i)
        if new_regime != _regime:
            _log(f"Rejim değişti: {_regime} → {new_regime} (skor={score:.1f})", "WARN")
        _regime = new_regime; _regime_score = score
        _cfg = RegimeConfig.for_regime(_regime)
        _regime_counter = 0

    # Pozisyon kontrolü
    if _position is not None:
        _check_exit_live(i)

    # Sinyal kontrolü (bot aktifse ve pozisyon yoksa)
    if _bot_running and not _bot_paused and _position is None and n > 50:
        _check_entry_signal(i)
    elif _bot_running and not _bot_paused and _position is None and n <= 50:
        _log(f"Yeterli mum yok: {n}/50", "WARN")

    # Broadcast
    _broadcast("candle_closed", {
        "candle": candle_raw,
        "regime": _regime,
        "regime_score": round(_regime_score, 2),
        "lines": _get_lines_dict(),
        "position": _get_position_dict(),
        "stats": _get_stats_dict(),
    })


def _to_candle(d: dict) -> Candle:
    return Candle(str(d["ts"]), d["open"], d["high"], d["low"], d["close"], d["volume"])


def _update_live_pnl(current_price: float):
    if _position:
        comm = COMMISSION * 2
        if _position.direction == "long":
            _position.pnl_pct = (current_price - _position.entry_price) / _position.entry_price - comm
        else:
            _position.pnl_pct = (_position.entry_price - current_price) / _position.entry_price - comm


# ─── Sinyal motoru ────────────────────────────────────────────────────────────

def _check_entry_signal(i: int):
    global _position, _stats

    c    = _candle_objs[i]
    cfg  = _cfg
    in_rng = is_range(_candle_objs, i)
    if in_rng:
        _log(f"RANGE: giriş yok @ {c.c:.2f}", "INFO")
        return

    MIN_TOUCH = _MIN_TOUCH; CONFIRM = _CONFIRM; VOL_MULT = _VOL_MULT
    MOM_K = _MOM_K; MOM_MIN = _MOM_MIN; BREAK_MARGIN = _BREAK_MARGIN

    va     = vol_avg(_candle_objs, i)
    vol_ok = va > 0 and c.v >= va * VOL_MULT
    mdir   = market_direction(_candle_objs, i, cfg.trend_filter_window)
    atr    = calc_atr(_candle_objs, i)

    if cfg.vol_filter_min > 0 and atr / c.c < cfg.vol_filter_min:
        _log(f"VOL_FILTER: atr/c={atr/c.c:.5f} < {cfg.vol_filter_min}", "INFO")
        return

    if i < CONFIRM:
        return

    va     = vol_avg(_candle_objs, i)
    vol_ok = va > 0 and c.v >= va * VOL_MULT
    if not vol_ok:
        _log(f"SCAN: C={c.c:.2f} vol={c.v:.0f}/avg={va:.0f}({c.v/va*100 if va>0 else 0:.0f}%) res={'T'+str(_res.touch_count) if _res else 'None'} sup={'T'+str(_sup.touch_count) if _sup else 'None'} mdir={market_direction(_candle_objs,i,cfg.trend_filter_window)}", "INFO")
    else:
        _log(f"SCAN✓: C={c.c:.2f} vol={c.v:.0f}/avg={va:.0f}({c.v/va*100:.0f}%) res={'T'+str(_res.touch_count) if _res else 'None'} sup={'T'+str(_sup.touch_count) if _sup else 'None'} mdir={market_direction(_candle_objs,i,cfg.trend_filter_window)}", "INFO")

    direction = None

    # LONG sinyali
    # lreq='any' = her piyasa yönünde long al (backtest motoruyla aynı davranış)
    # lreq='up'  = sadece yükselen trendde long al
    if _res and _res.touch_count >= MIN_TOUCH:
        la = True
        if cfg.long_trend_req == "up" and mdir != "up":
            la = False
        if la:
            all_above = all(_candle_objs[j].c > _res.price_at(j) * (1 + BREAK_MARGIN)
                            for j in range(i - CONFIRM + 1, i + 1))
            fb = _candle_objs[i - CONFIRM].c <= _res.price_at(i - CONFIRM)
            def mom_ok(d):
                w = _candle_objs[max(0, i - MOM_K):i + 1]
                if d == "long": return sum(1 for c in w if c.c >= c.o) / len(w) >= MOM_MIN
                return sum(1 for c in w if c.c < c.o) / len(w) >= MOM_MIN
            if all_above and fb and vol_ok and mom_ok("long"):
                direction = "long"

    # SHORT sinyali
    # sreq='down' = sadece düşen trendde short al
    # sreq='any'  = her piyasa yönünde short al
    if direction is None and not cfg.long_only and _sup and _sup.touch_count >= MIN_TOUCH:
        sa = True
        if cfg.short_trend_req == "down" and mdir != "down":
            sa = False
        if sa:
            def mom_ok(d):
                w = _candle_objs[max(0, i - MOM_K):i + 1]
                if d == "long": return sum(1 for c in w if c.c >= c.o) / len(w) >= MOM_MIN
                return sum(1 for c in w if c.c < c.o) / len(w) >= MOM_MIN
            all_below = all(_candle_objs[j].c < _sup.price_at(j) * (1 - BREAK_MARGIN)
                            for j in range(i - CONFIRM + 1, i + 1))
            fb = _candle_objs[i - CONFIRM].c >= _sup.price_at(i - CONFIRM)
            if all_below and fb and vol_ok and mom_ok("short"):
                direction = "short"

    if not direction:
        return

    ep   = c.c
    bl   = _res if direction == "long" else _sup
    stop = calc_dynamic_stop(direction, ep, bl, i)
    sl_d = abs(ep - stop) / ep
    min_tp = abs(ep - stop) * cfg.tp1_min_rr
    atr_v  = calc_atr(_candle_objs, i)
    tp1  = calc_tp1(direction, ep, i, _res, _sup, _ph, _pl, _candle_objs, atr_v, min_tp)
    tp_d = abs(tp1 - ep) / ep
    rr   = tp_d / sl_d if sl_d > 0 else 0

    if rr < cfg.tp1_min_rr:
        return

    _position = LivePosition(
        direction=direction, entry_price=ep, stop=stop, tp1=tp1,
        size=cfg.position_size, best_price=ep,
        entry_ts=int(c.ts), entry_time=datetime.now().strftime("%H:%M:%S"),
    )
    _stats.last_signal_time = datetime.now().strftime("%H:%M:%S")

    _log(f"GİRİŞ {direction.upper()} @ {ep:.2f}  SL={stop:.2f}({sl_d*100:.2f}%)  "
         f"TP1={tp1:.2f}({tp_d*100:.2f}%)  R/R={rr:.2f}  Rejim={_regime}", "SIGNAL")
    _broadcast("signal", {"direction": direction, "entry": ep,
                           "stop": stop, "tp1": tp1, "rr": round(rr, 2),
                           "regime": _regime})


def _check_exit_live(i: int):
    global _position, _stats, _equity_curve

    pos = _position
    c   = _candle_objs[i]
    cfg = _cfg
    BREAK_MARGIN = 0.0005

    # Trailing güncelle
    if pos.half_closed:
        if pos.direction == "long":
            if c.h > pos.best_price:
                pos.best_price = c.h
                nt = pos.best_price * (1 - cfg.trailing_step)
                if nt > pos.stop: pos.stop = nt
        else:
            if pos.best_price == 0 or c.l < pos.best_price:
                pos.best_price = c.l
                nt = pos.best_price * (1 + cfg.trailing_step)
                if nt < pos.stop: pos.stop = nt

    result = None
    if pos.direction == "long":
        if c.l <= pos.stop:
            result = ("trailing_stop" if pos.half_closed else "stop", pos.stop)
        elif not pos.half_closed and c.h >= pos.tp1:
            result = ("tp1_half", pos.tp1)
        elif _res and c.c < _res.price_at(i) * (1 - BREAK_MARGIN):
            result = ("reverse_break", c.c)
        elif not _res and not pos.half_closed:
            result = ("line_invalid", c.c)
    else:
        if c.h >= pos.stop:
            result = ("trailing_stop" if pos.half_closed else "stop", pos.stop)
        elif not pos.half_closed and c.l <= pos.tp1:
            result = ("tp1_half", pos.tp1)
        elif _sup and c.c > _sup.price_at(i) * (1 + BREAK_MARGIN):
            result = ("reverse_break", c.c)
        elif not _sup and not pos.half_closed:
            result = ("line_invalid", c.c)

    if not result:
        return

    reason, exit_price = result

    if reason == "tp1_half":
        pos.half_closed = True
        pos.stop = pos.entry_price
        pos.best_price = exit_price
        pnl = _pnl(pos.direction, pos.entry_price, exit_price)
        _record_trade(pos, exit_price, "tp1", pnl, 0.5)
        _log(f"TP1 (yarı) @ {exit_price:.2f}  PnL={pnl*100:+.3f}%  Trailing aktif", "PROFIT" if pnl > 0 else "LOSS")
    else:
        sz  = 0.5 if pos.half_closed else 1.0
        pnl = _pnl(pos.direction, pos.entry_price, exit_price)
        _record_trade(pos, exit_price, reason, pnl, sz)
        sign = "PROFIT" if pnl > 0 else "LOSS"
        _log(f"KAPAT({reason}) @ {exit_price:.2f}  PnL={pnl*100:+.3f}%  Equity={_stats.equity:.4f}", sign)
        _position = None

    # Equity kaydet
    _equity_curve.append({"ts": int(time.time() * 1000), "equity": round(_stats.equity, 5)})
    if len(_equity_curve) > 2000: _equity_curve.pop(0)


def _record_trade(pos: LivePosition, exit_price: float, reason: str, pnl: float, size: float):
    global _stats, _closed_trades

    _stats.equity *= (1 + pnl * size * pos.size)
    if _stats.equity > _stats.peak_equity:
        _stats.peak_equity = _stats.equity
    dd = (_stats.peak_equity - _stats.equity) / _stats.peak_equity
    if dd > _stats.max_drawdown: _stats.max_drawdown = dd

    if reason not in ("tp1",):  # tp1_half sadece ara kayıt
        _stats.total_trades += 1
        if pnl > 0: _stats.wins += 1
        else:       _stats.losses += 1
        _stats.total_pnl += pnl * size * pos.size

    _closed_trades.append(ClosedTrade(
        direction=pos.direction, entry_price=pos.entry_price,
        exit_price=exit_price, entry_time=pos.entry_time,
        exit_time=datetime.now().strftime("%H:%M:%S"),
        exit_reason=reason, pnl_pct=pnl, size=size, regime=_regime,
    ))
    if len(_closed_trades) > 1000: _closed_trades.pop(0)


# ─── Yardımcı getterlar ───────────────────────────────────────────────────────

def _get_lines_dict() -> dict:
    n = len(_candle_objs)
    # Frontend'e son 200 mum gönderiliyor (get_state: _candles[-200:])
    # points indeksleri bu 200 mumun 0-based indeksleri olmalı
    tail = 200
    start_abs = max(0, n - tail)  # candle_objs'daki başlangıç indeksi

    def tl_to_dict(tl: Optional[TrendLine]) -> Optional[dict]:
        if tl is None: return None
        points = []
        for abs_i in range(start_abs, n):
            rel_i = abs_i - start_abs   # 0..199 arası (frontend S.candles indeksi)
            price = round(tl.price_at(abs_i), 4)
            ts    = _candles[abs_i]["ts"] if abs_i < len(_candles) else 0
            points.append({"i": rel_i, "ts": ts, "price": price})
        return {
            "type": tl.type, "touch_count": tl.touch_count, "age": tl.age,
            "slope": round(tl.slope(), 8), "points": points,
        }

    return {"res": tl_to_dict(_res), "sup": tl_to_dict(_sup)}


def _get_position_dict() -> Optional[dict]:
    if _position is None: return None
    return {
        "direction": _position.direction, "entry_price": _position.entry_price,
        "stop": _position.stop, "tp1": _position.tp1,
        "half_closed": _position.half_closed, "pnl_pct": round(_position.pnl_pct * 100, 4),
        "entry_time": _position.entry_time, "size": _position.size,
    }


def _get_stats_dict() -> dict:
    return {
        "total_trades": _stats.total_trades, "wins": _stats.wins,
        "losses": _stats.losses, "win_rate": round(_stats.win_rate, 2),
        "equity": round(_stats.equity, 5),
        "total_pnl_pct": round(_stats.total_pnl * 100, 3),
        "max_drawdown": round(_stats.max_drawdown * 100, 3),
        "profit_factor": round(_stats.profit_factor, 3),
        "running_since": _stats.running_since,
        "last_signal": _stats.last_signal_time,
    }


# ─── Public API ───────────────────────────────────────────────────────────────

def get_state() -> dict:
    return {
        "candles":   _candles[-200:],
        "regime":    _regime,
        "regime_score": round(_regime_score, 2),
        "lines":     _get_lines_dict(),
        "position":  _get_position_dict(),
        "stats":     _get_stats_dict(),
        "equity_curve": _equity_curve[-500:],
        "trades":    [asdict(t) for t in _closed_trades[-100:]],
        "logs":      _log_lines[-100:],
        "running":   _bot_running,
        "paused":    _bot_paused,
        "cfg":       _cfg.__dict__,
    }

def get_logs() -> list:
    return _log_lines[-200:]

def get_trades_csv() -> str:
    import io, csv as cmod
    buf = io.StringIO()
    if not _closed_trades: return ""
    fields = list(asdict(_closed_trades[0]).keys())
    w = cmod.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    for t in _closed_trades:
        w.writerow(asdict(t))
    return buf.getvalue()

def reset_history():
    """TF değiştiğinde state'i sıfırla."""
    global _candles, _candle_objs, _ph, _pl, _res, _sup
    global _last_line_update, _regime_counter, _position
    _candles.clear(); _candle_objs.clear()
    _ph.clear(); _pl.clear()
    _res = _sup = None
    _last_line_update = 0; _regime_counter = 0
    _position = None
    _log("Geçmiş sıfırlandı (TF değişimi)", "INFO")


def start_bot():
    global _bot_running, _bot_paused
    _bot_running = True; _bot_paused = False
    # Son kapanan mumu hemen tara — bot başlar başlamaz sinyal motoru aktif olsun
    if _candles:
        last_closed = next((c for c in reversed(_candles) if c.get('closed', True)), None)
        if last_closed:
            _log(f"[BOT] Başlatıldı, anlık tarama: C={last_closed['close']:.2f}", "INFO")
            _check_entry_signal(len(_candle_objs) - 1)
    _log("Bot BAŞLATILDI", "INFO")
    _broadcast("bot_status", {"running": True, "paused": False})

def stop_bot():
    global _bot_running
    _bot_running = False
    _log("Bot DURDURULDU", "WARN")
    _broadcast("bot_status", {"running": False, "paused": False})

def pause_bot():
    global _bot_paused
    _bot_paused = not _bot_paused
    state = "DURAKLATILDI" if _bot_paused else "DEVAM"
    _log(f"Bot {state}", "WARN")
    _broadcast("bot_status", {"running": _bot_running, "paused": _bot_paused})

def close_position_manual(reason: str = "manual"):
    global _position
    if _position is None: return {"error": "Açık pozisyon yok"}
    if not _candle_objs:   return {"error": "Veri yok"}
    last_price = _candle_objs[-1].c
    sz = 0.5 if _position.half_closed else 1.0
    pnl = _pnl(_position.direction, _position.entry_price, last_price)
    _record_trade(_position, last_price, reason, pnl, sz)
    _log(f"Manuel kapatma @ {last_price:.2f}  PnL={pnl*100:+.3f}%", "WARN")
    _position = None
    return {"ok": True, "pnl_pct": round(pnl * 100, 4)}

def update_config(regime: str = None, params: dict = None):
    global _regime, _cfg
    if regime: _regime = regime
    if params:
        for k, v in params.items():
            if hasattr(_cfg, k): setattr(_cfg, k, v)
    _log(f"Config güncellendi: rejim={_regime} TFW={_cfg.trend_filter_window}", "INFO")
    _broadcast("config_update", {"regime": _regime, "cfg": _cfg.__dict__})
