"""
engine.py — Adaptif Trend Botu Motoru
======================================
Grid search bulgularından türetilen formüller:
  • TFW=150, vol=0.003, trail=0.005, rr=1.5, long_req=up, short_req=down  → 4/5 ay kârlı
  • Piyasa rejimine göre parametreler otomatik ayarlanır
  • Her ayın kendi "en iyi" konfigürasyonu farklı — ajan bu seçimi dinamik yapar

Rejim → Parametre Haritası (backtest verisiyle öğrenildi):
  BEAR_STRONG  → TFW=80,  trail=0.003, lreq=any,  sreq=down  (short ağırlıklı)
  BEAR         → TFW=100, trail=0.003, lreq=up,   sreq=down  (dengeli, dikkatli)
  NEUTRAL      → TFW=150, trail=0.005, lreq=up,   sreq=down  (en seçici)
  BULL         → TFW=100, trail=0.005, lreq=any,  sreq=any   (long ağırlıklı)
  BULL_STRONG  → TFW=80,  trail=0.005, lreq=any,  sreq=any   (agresif long)
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# VERİ YAPILARI
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    ts: str; o: float; h: float; l: float; c: float; v: float

@dataclass
class TrendLine:
    type: str; i1: int; p1: float; i2: int; p2: float
    touch_count: int = 2; age: int = 0

    def price_at(self, i: int) -> float:
        return self.p1 + (self.p2 - self.p1) / max(self.i2 - self.i1, 1) * (i - self.i1)

    def slope(self) -> float:
        return (self.p2 - self.p1) / max(self.i2 - self.i1, 1)

@dataclass
class Position:
    direction: str; entry_i: int; entry_price: float
    stop: float; tp1: float; tp2: float
    size: float = 1.0; half_closed: bool = False
    be_activated: bool = False; best_price: float = 0.0

@dataclass
class Trade:
    direction: str; entry_i: int; entry_ts: str; entry_price: float
    exit_i: int; exit_ts: str; exit_price: float; exit_reason: str
    pnl_pct: float; size: float; regime: str = "NEUTRAL"

@dataclass
class RegimeConfig:
    """Rejime göre otomatik seçilen parametreler"""
    trend_filter_window: int = 150
    trailing_step: float = 0.005
    tp1_min_rr: float = 1.5
    long_trend_req: str = 'up'    # 'up' | 'any'
    short_trend_req: str = 'down' # 'down' | 'any'
    long_only: bool = False
    vol_filter_min: float = 0.003
    position_size: float = 1.0    # 0.5 = half size in uncertain regimes

    @classmethod
    def for_regime(cls, regime: str) -> 'RegimeConfig':
        """Backtest bulgularıyla eşleşen rejim→parametre haritası"""
        maps = {
            'BEAR_STRONG': cls(trend_filter_window=80,  trailing_step=0.003, tp1_min_rr=1.5,
                               long_trend_req='any', short_trend_req='down', long_only=False,
                               vol_filter_min=0.003, position_size=0.8),
            'BEAR':        cls(trend_filter_window=100, trailing_step=0.003, tp1_min_rr=1.5,
                               long_trend_req='up',  short_trend_req='down', long_only=False,
                               vol_filter_min=0.002, position_size=0.9),
            'NEUTRAL':     cls(trend_filter_window=150, trailing_step=0.005, tp1_min_rr=1.5,
                               long_trend_req='up',  short_trend_req='down', long_only=False,
                               vol_filter_min=0.003, position_size=1.0),
            'BULL':        cls(trend_filter_window=100, trailing_step=0.005, tp1_min_rr=1.5,
                               long_trend_req='any', short_trend_req='any',  long_only=False,
                               vol_filter_min=0.002, position_size=1.0),
            'BULL_STRONG': cls(trend_filter_window=80,  trailing_step=0.005, tp1_min_rr=1.5,
                               long_trend_req='any', short_trend_req='any',  long_only=False,
                               vol_filter_min=0.0015, position_size=1.0),
        }
        return maps.get(regime, maps['NEUTRAL'])


# ─────────────────────────────────────────────────────────────────────────────
# REJİM DEDEKTÖRÜ
# ─────────────────────────────────────────────────────────────────────────────

def detect_regime(candles: list[Candle], end_i: int,
                  window: int = 2880) -> tuple[str, float]:
    """
    Piyasa rejimini 3 göstergeyle tespit et:
      1. Net fiyat değişimi (son window mumda)
      2. Higher-Highs / Lower-Lows pattern skoru
      3. EMA fast/slow bias

    Skor > +4  → BULL_STRONG
    Skor > +1  → BULL
    Skor < -4  → BEAR_STRONG
    Skor < -1  → BEAR
    Diğer      → NEUTRAL
    """
    start = max(0, end_i - window)
    w = candles[start:end_i + 1]
    if len(w) < 100:
        return 'NEUTRAL', 0.0

    # 1. Net fiyat değişimi
    price_chg = (w[-1].c - w[0].c) / w[0].c

    # 2. HH/LL pattern — 50'şer mumlu bloklarda
    step = 50; hh = 0; ll = 0
    prev_h = w[0].h; prev_l = w[0].l
    for i in range(step, len(w), step):
        blk = w[i:i + step]
        if not blk: break
        bh = max(c.h for c in blk)
        bl = min(c.l for c in blk)
        if bh > prev_h: hh += 1
        if bl < prev_l: ll += 1
        prev_h, prev_l = bh, bl
    total_b = max(1, hh + ll)
    pattern_score = (hh - ll) / total_b  # [-1, +1]

    # 3. EMA bias (8h fast, 24h slow → 480/1440 mum)
    ema_bias = 0.0
    if end_i >= 1440:
        ema_fast = sum(c.c for c in candles[end_i - 480:end_i]) / 480
        ema_slow = sum(c.c for c in candles[end_i - 1440:end_i]) / 1440
        ema_bias = (ema_fast - ema_slow) / ema_slow

    # Kombine skor
    score = price_chg * 100 + pattern_score * 3 + ema_bias * 200

    if score > 4:   return 'BULL_STRONG', score
    if score > 1:   return 'BULL', score
    if score < -4:  return 'BEAR_STRONG', score
    if score < -1:  return 'BEAR', score
    return 'NEUTRAL', score


# ─────────────────────────────────────────────────────────────────────────────
# YARDIMCI FONKSİYONLAR
# ─────────────────────────────────────────────────────────────────────────────

def calc_atr(candles: list[Candle], i: int, window: int = 14) -> float:
    start = max(1, i - window + 1); trs = []
    for j in range(start, i + 1):
        tr = max(candles[j].h - candles[j].l,
                 abs(candles[j].h - candles[j-1].c),
                 abs(candles[j].l - candles[j-1].c))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else candles[i].h - candles[i].l

def market_direction(candles: list[Candle], i: int, window: int) -> str:
    start = max(0, i - window)
    chg = (candles[i].c - candles[start].c) / candles[start].c
    if chg > 0.005: return 'up'
    if chg < -0.005: return 'down'
    return 'neutral'

def vol_avg(candles: list[Candle], i: int, lb: int = 20) -> float:
    start = max(0, i - lb)
    vols = [candles[j].v for j in range(start, i)]
    return sum(vols) / len(vols) if vols else 0

def is_pivot_high(candles: list[Candle], i: int, n: int) -> bool:
    if i < n or i >= len(candles) - n: return False
    h = candles[i].h
    for j in range(i - n, i + n + 1):
        if j != i and candles[j].h >= h: return False
    return True

def is_pivot_low(candles: list[Candle], i: int, n: int) -> bool:
    if i < n or i >= len(candles) - n: return False
    l = candles[i].l
    for j in range(i - n, i + n + 1):
        if j != i and candles[j].l <= l: return False
    return True

def build_trend_line(candles, ia, ib, lt, ci, max_age=80, tol=0.002):
    if lt == 'res': p1, p2 = candles[ia].h, candles[ib].h
    else: p1, p2 = candles[ia].l, candles[ib].l
    tl = TrendLine(type=lt, i1=ia, p1=p1, i2=ib, p2=p2)
    if lt == 'res' and tl.slope() > 0: return None
    if lt == 'sup' and tl.slope() < 0: return None
    tc = 2; ss = max(ia + 1, ci - max_age)
    for j in range(ss, ci + 1):
        lp = tl.price_at(j); t = lp * tol
        if lt == 'res':
            if candles[j].c > lp + t: return None
            if abs(candles[j].h - lp) <= t: tc += 1
        else:
            if candles[j].c < lp - t: return None
            if abs(candles[j].l - lp) <= t: tc += 1
    tl.touch_count = tc; tl.age = ci - ib
    return tl

def get_best_lines(ph, pl, candles, up_to, pivot_n=2, max_age=80, tol=0.002):
    best_res = best_sup = None
    rph = [x for x in ph if x <= up_to - pivot_n][-4:]
    cr = []
    for i in range(len(rph) - 1, max(len(rph) - 4, 0) - 1, -1):
        for j in range(i - 1, max(i - 3, -1), -1):
            tl = build_trend_line(candles, rph[j], rph[i], 'res', up_to, max_age, tol)
            if tl and tl.age <= max_age: cr.append(tl)
    if cr: best_res = max(cr, key=lambda t: (t.touch_count, -t.age))
    rpl = [x for x in pl if x <= up_to - pivot_n][-4:]
    cs = []
    for i in range(len(rpl) - 1, max(len(rpl) - 4, 0) - 1, -1):
        for j in range(i - 1, max(i - 3, -1), -1):
            tl = build_trend_line(candles, rpl[j], rpl[i], 'sup', up_to, max_age, tol)
            if tl and tl.age <= max_age: cs.append(tl)
    if cs: best_sup = max(cs, key=lambda t: (t.touch_count, -t.age))
    return best_res, best_sup

def is_range(candles, up_to, rw=15, rt=0.010):
    if up_to < rw: return True
    w = candles[up_to - rw:up_to + 1]
    hi = max(c.h for c in w); lo = min(c.l for c in w)
    return (hi - lo) / lo < rt

def calc_dynamic_stop(direction, entry, bl, ei, sl_buffer=0.002, sl_pct=0.005):
    fb = entry * (1 - sl_pct) if direction == 'long' else entry * (1 + sl_pct)
    if bl is None: return fb
    lp = bl.price_at(ei)
    if direction == 'long':
        dyn = lp * (1 - sl_buffer)
        if dyn < entry * (1 - 0.012): return fb
        return max(dyn, fb)
    else:
        dyn = lp * (1 + sl_buffer)
        if dyn > entry * (1 + 0.012): return fb
        return min(dyn, fb)

def calc_tp1(direction, entry, ei, res, sup, ph, pl, candles, atr, min_tp_dist):
    cands = []
    if direction == 'long':
        above = [candles[idx].h for idx in ph if candles[idx].h > entry * 1.002]
        if above: cands.append(min(above))
        if sup:
            sp = sup.price_at(ei)
            if sp > entry * 1.002: cands.append(sp)
        cands.append(entry + atr * 1.5)
        valid = [c for c in cands if (c - entry) >= min_tp_dist]
        return min(valid) if valid else entry + min_tp_dist
    else:
        below = [candles[idx].l for idx in pl if candles[idx].l < entry * 0.998]
        if below: cands.append(max(below))
        if res:
            rp = res.price_at(ei)
            if rp < entry * 0.998: cands.append(rp)
        cands.append(entry - atr * 1.5)
        valid = [c for c in cands if (entry - c) >= min_tp_dist]
        return max(valid) if valid else entry - min_tp_dist

def _pnl(direction, entry, exit_p, commission=0.0004):
    comm = commission * 2
    return (exit_p - entry) / entry - comm if direction == 'long' \
           else (entry - exit_p) / entry - comm


# ─────────────────────────────────────────────────────────────────────────────
# ANA BACKTEST MOTORU — Adaptif Rejim ile
# ─────────────────────────────────────────────────────────────────────────────

REGIME_UPDATE_INTERVAL = 2880   # Her 2 günde bir rejim güncelle
COMMISSION = 0.0004
PIVOT_N = 2; MIN_TOUCH = 3; CONFIRM = 2
VOL_MULT = 1.1; MOM_K = 3; MOM_MIN = 0.55; BREAK_MARGIN = 0.0005


def run_adaptive_backtest(candles: list[Candle], initial_regime: str = 'NEUTRAL',
                          fixed_config: Optional[RegimeConfig] = None) -> dict:
    """
    Adaptif backtest: her REGIME_UPDATE_INTERVAL mumda rejimi yeniden hesaplar,
    parametreleri otomatik günceller.
    fixed_config verilirse adaptasyon kapalı (karşılaştırma için).
    """
    n = len(candles)
    trades: list[Trade] = []; equity = 1.0; equity_curve = [1.0]

    ph: list[int] = []; pl: list[int] = []
    cached_res = cached_sup = None; last_line_update = 0

    min_candles = max(PIVOT_N * 2 + 2, 16)

    state = 'range'; pos: Optional[Position] = None
    entry_res = entry_sup = None

    # Rejim takibi
    current_regime = initial_regime
    current_cfg = fixed_config if fixed_config else RegimeConfig.for_regime(current_regime)
    last_regime_update = 0

    regime_log: list[dict] = []   # rejim geçiş kaydı

    def mom_ok(i, d):
        s = max(0, i - MOM_K); w = candles[s:i + 1]
        if not w: return False
        if d == 'long': return sum(1 for c in w if c.c >= c.o) / len(w) >= MOM_MIN
        else: return sum(1 for c in w if c.c < c.o) / len(w) >= MOM_MIN

    for i in range(min_candles, n):
        c = candles[i]

        # Pivot güncelle
        ci = i - PIVOT_N
        if ci >= PIVOT_N:
            if is_pivot_high(candles, ci, PIVOT_N): ph.append(ci)
            if is_pivot_low(candles, ci, PIVOT_N): pl.append(ci)

        # Çizgi güncelle (her 5 mumda bir)
        if i - last_line_update >= 5 or cached_res is None:
            cached_res, cached_sup = get_best_lines(ph, pl, candles, i)
            last_line_update = i

        # Rejim güncelle (sabit config yoksa)
        if fixed_config is None and i - last_regime_update >= REGIME_UPDATE_INTERVAL:
            new_regime, score = detect_regime(candles, i)
            if new_regime != current_regime:
                regime_log.append({
                    'candle_i': i, 'ts': c.ts,
                    'from': current_regime, 'to': new_regime, 'score': round(score, 3)
                })
                current_regime = new_regime
                current_cfg = RegimeConfig.for_regime(current_regime)
            last_regime_update = i

        cfg = current_cfg
        res, sup = cached_res, cached_sup
        in_rng = is_range(candles, i)
        mdir = market_direction(candles, i, cfg.trend_filter_window)
        atr = calc_atr(candles, i)

        # ── IN_POSITION ──────────────────────────────────────────────────────
        if state == 'in_position' and pos is not None:
            # Trailing stop güncelle
            if pos.half_closed:
                if pos.direction == 'long':
                    if c.h > pos.best_price:
                        pos.best_price = c.h
                        nt = pos.best_price * (1 - cfg.trailing_step)
                        if nt > pos.stop: pos.stop = nt
                else:
                    if pos.best_price == 0.0 or c.l < pos.best_price:
                        pos.best_price = c.l
                        nt = pos.best_price * (1 + cfg.trailing_step)
                        if nt < pos.stop: pos.stop = nt

            result = None; er = entry_res; es = entry_sup
            if pos.direction == 'long':
                if c.l <= pos.stop:
                    result = ('trailing_stop' if pos.half_closed else 'stop', pos.stop)
                elif not pos.half_closed and c.h >= pos.tp1:
                    result = ('tp1_half', pos.tp1)
                elif er and c.c < er.price_at(i) * (1 - BREAK_MARGIN):
                    result = ('reverse_break', c.c)
                elif not er and not pos.half_closed:
                    result = ('line_invalid', c.c)
            else:
                if c.h >= pos.stop:
                    result = ('trailing_stop' if pos.half_closed else 'stop', pos.stop)
                elif not pos.half_closed and c.l <= pos.tp1:
                    result = ('tp1_half', pos.tp1)
                elif es and c.c > es.price_at(i) * (1 + BREAK_MARGIN):
                    result = ('reverse_break', c.c)
                elif not es and not pos.half_closed:
                    result = ('line_invalid', c.c)

            if result:
                reason, exit_price = result
                if reason == 'tp1_half':
                    pos.half_closed = True; pos.stop = pos.entry_price; pos.best_price = exit_price
                    pnl = _pnl(pos.direction, pos.entry_price, exit_price)
                    trades.append(Trade(pos.direction, pos.entry_i, candles[pos.entry_i].ts,
                                        pos.entry_price, i, c.ts, exit_price, 'tp1', pnl, 0.5,
                                        regime=current_regime))
                    equity *= (1 + pnl * 0.5 * cfg.position_size)
                else:
                    sz = 0.5 if pos.half_closed else 1.0
                    pnl = _pnl(pos.direction, pos.entry_price, exit_price)
                    trades.append(Trade(pos.direction, pos.entry_i, candles[pos.entry_i].ts,
                                        pos.entry_price, i, c.ts, exit_price, reason, pnl, sz,
                                        regime=current_regime))
                    equity *= (1 + pnl * sz * cfg.position_size)
                    pos = None; state = 'range' if in_rng else 'scanning'

        # ── SCANNING ─────────────────────────────────────────────────────────
        elif state == 'scanning':
            if in_rng:
                state = 'range'
            elif i >= CONFIRM:
                va = vol_avg(candles, i)
                vol_ok = va > 0 and c.v >= va * VOL_MULT

                # Volatilite filtresi
                if cfg.vol_filter_min > 0 and atr / c.c < cfg.vol_filter_min:
                    pass  # düşük vol, giriş yapma
                else:
                    direction = None

                    # LONG sinyali
                    if res and res.touch_count >= MIN_TOUCH:
                        la = True
                        if cfg.long_trend_req == 'up' and mdir != 'up': la = False
                        elif mdir == 'down' and cfg.long_trend_req == 'any': la = False
                        if la:
                            all_above = all(candles[j].c > res.price_at(j) * (1 + BREAK_MARGIN)
                                            for j in range(i - CONFIRM + 1, i + 1))
                            fb = candles[i - CONFIRM].c <= res.price_at(i - CONFIRM)
                            if all_above and fb and vol_ok and mom_ok(i, 'long'):
                                direction = 'long'

                    # SHORT sinyali
                    if direction is None and not cfg.long_only and sup and sup.touch_count >= MIN_TOUCH:
                        sa = True
                        if cfg.short_trend_req == 'down' and mdir != 'down': sa = False
                        elif mdir == 'up' and cfg.short_trend_req == 'any': sa = False
                        if sa:
                            all_below = all(candles[j].c < sup.price_at(j) * (1 - BREAK_MARGIN)
                                            for j in range(i - CONFIRM + 1, i + 1))
                            fb = candles[i - CONFIRM].c >= sup.price_at(i - CONFIRM)
                            if all_below and fb and vol_ok and mom_ok(i, 'short'):
                                direction = 'short'

                    if direction:
                        ep = c.c; bl = res if direction == 'long' else sup
                        stop = calc_dynamic_stop(direction, ep, bl, i)
                        sl_d = abs(ep - stop) / ep
                        min_tp = abs(ep - stop) * cfg.tp1_min_rr
                        tp1 = calc_tp1(direction, ep, i, res, sup, ph, pl, candles, atr, min_tp)
                        tp_d = abs(tp1 - ep) / ep
                        rr = tp_d / sl_d if sl_d > 0 else 0
                        if rr >= cfg.tp1_min_rr:
                            pos = Position(direction, i, ep, stop, tp1, 0, best_price=ep)
                            entry_res = res; entry_sup = sup; state = 'in_position'

        # ── RANGE ────────────────────────────────────────────────────────────
        elif state == 'range':
            if not in_rng: state = 'scanning'

        equity_curve.append(round(equity, 6))

    # Açık pozisyonu kapat
    if pos and state == 'in_position':
        last = candles[-1]; sz = 0.5 if pos.half_closed else 1.0
        pnl = _pnl(pos.direction, pos.entry_price, last.c)
        trades.append(Trade(pos.direction, pos.entry_i, candles[pos.entry_i].ts,
                            pos.entry_price, n - 1, last.ts, last.c, 'end_of_data', pnl, sz,
                            regime=current_regime))
        equity *= (1 + pnl * sz); equity_curve[-1] = round(equity, 6)

    return {
        'trades': trades,
        'equity_curve': equity_curve,
        'final_equity': round(equity, 5),
        'total_return_pct': round((equity - 1) * 100, 3),
        'regime_log': regime_log,
        'current_regime': current_regime,
        'metrics': _compute_metrics(trades, equity_curve),
    }


def _compute_metrics(trades: list[Trade], equity_curve: list[float]) -> dict:
    if not trades:
        return {'total_trades': 0, 'win_rate': 0, 'profit_factor': 0,
                'max_drawdown_pct': 0, 'expectancy_pct': 0, 'total_return_pct': 0}
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    total = len(trades); wr = len(wins) / total
    aw = sum(t.pnl_pct for t in wins) / len(wins) if wins else 0
    al = sum(t.pnl_pct for t in losses) / len(losses) if losses else 0
    tg = sum(t.pnl_pct * t.size for t in wins)
    tl = abs(sum(t.pnl_pct * t.size for t in losses))
    pf = tg / tl if tl > 0 else 999
    peak = 1.0; max_dd = 0.0
    for e in equity_curve:
        if e > peak: peak = e
        dd = (peak - e) / peak
        if dd > max_dd: max_dd = dd
    exp = wr * aw + (1 - wr) * al
    # Rejime göre kırılım
    by_regime: dict[str, dict] = {}
    for t in trades:
        r = t.regime
        if r not in by_regime: by_regime[r] = {'wins': 0, 'losses': 0, 'pnl': 0}
        if t.pnl_pct > 0: by_regime[r]['wins'] += 1
        else: by_regime[r]['losses'] += 1
        by_regime[r]['pnl'] += t.pnl_pct * t.size
    return {
        'total_trades': total, 'win_rate': round(wr * 100, 2),
        'avg_win_pct': round(aw * 100, 4), 'avg_loss_pct': round(al * 100, 4),
        'expectancy_pct': round(exp * 100, 4), 'profit_factor': round(pf, 3),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'total_return_pct': round((equity_curve[-1] - 1) * 100, 3),
        'by_regime': {k: {'wr': round(v['wins']/max(1,v['wins']+v['losses'])*100,1),
                          'pnl': round(v['pnl']*100,3),
                          'trades': v['wins']+v['losses']} for k,v in by_regime.items()},
        'exit_reasons': {r: sum(1 for t in trades if t.exit_reason == r)
                         for r in set(t.exit_reason for t in trades)},
    }
