import os
import time
import math
import requests
import pandas as pd

from datetime import datetime, timezone
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None

from ta.trend import EMAIndicator, ADXIndicator, MACD, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import ChaikinMoneyFlowIndicator, OnBalanceVolumeIndicator


# ============================================================
# CONFIG
# ============================================================
PROXY_BASE = "https://binance-proxy-63js.onrender.com/fapi"
FUTURES_BASE = PROXY_BASE

INTERVAL = os.getenv("INTERVAL", "1h")   # 15m / 1h / 4h / 1d
LIMIT = int(os.getenv("LIMIT", "300"))
SLEEP = float(os.getenv("SLEEP", "0.02"))

# Global filters
SIDE_FILTER = os.getenv("SIDE", "ALL").upper()  # ALL / LONG / SHORT
MIN_RR = float(os.getenv("MIN_RR", "1.2"))
MIN_ADX = float(os.getenv("MIN_ADX", "15"))

# ATR risk
ATR_STOP_MULT = float(os.getenv("ATR_STOP", "1.5"))
ATR_TP_MULT = float(os.getenv("ATR_TP", "2.5"))

# Strategy-specific knobs
# Trend/Momentum (opt)
TM_REQUIRE_SQUEEZE = os.getenv("TM_REQUIRE_SQUEEZE", "1") == "1"
TM_MAX_BB_WIDTH = float(os.getenv("TM_MAX_BB_WIDTH", "0.085"))
TM_MAX_ATR_PCT = float(os.getenv("TM_MAX_ATR_PCT", "6.5"))
TM_RSI_LONG_MIN = float(os.getenv("TM_RSI_LONG_MIN", "45"))
TM_RSI_LONG_MAX = float(os.getenv("TM_RSI_LONG_MAX", "66"))
TM_RSI_SHORT_MIN = float(os.getenv("TM_RSI_SHORT_MIN", "34"))
TM_RSI_SHORT_MAX = float(os.getenv("TM_RSI_SHORT_MAX", "55"))
TM_VOL_MIN_RATIO = float(os.getenv("TM_VOL_MIN_RATIO", "0.95"))      # volume >= 0.95 * avg
TM_CMF_LONG_MIN = float(os.getenv("TM_CMF_LONG_MIN", "-0.02"))
TM_CMF_SHORT_MAX = float(os.getenv("TM_CMF_SHORT_MAX", "0.02"))
TM_MIN_SCORE = int(os.getenv("TM_MIN_SCORE", "5"))                   # more selective
TM_MAX_ROWS = int(os.getenv("TM_MAX_ROWS", "80"))

# Breakout+Retest
BR_LOOKBACK = int(os.getenv("BR_LOOKBACK", "50"))      # breakout level lookback
BR_RETEST_ATR = float(os.getenv("BR_RETEST_ATR", "0.35"))  # retest tolerance (ATR fraction)
BR_REQUIRE_SQUEEZE = os.getenv("BR_REQUIRE_SQUEEZE", "1") == "1"
BR_MAX_BB_WIDTH = float(os.getenv("BR_MAX_BB_WIDTH", "0.07"))
BR_MAX_ATR_PCT = float(os.getenv("BR_MAX_ATR_PCT", "5.0"))
BR_MIN_SCORE = int(os.getenv("BR_MIN_SCORE", "6"))
BR_MAX_ROWS = int(os.getenv("BR_MAX_ROWS", "80"))

STABLECOINS = {"USDT", "BUSD", "DAI", "USDC", "TUSD", "FDUSD"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CryptoDailyBot/1.0)",
    "Accept": "application/json,text/plain,*/*",
}


# ============================================================
# Time helper (TSİ)
# ============================================================
def get_updated_at_str():
    now_utc = datetime.now(timezone.utc)
    if ZoneInfo is not None:
        try:
            ist = now_utc.astimezone(ZoneInfo("Europe/Istanbul"))
            return ist.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            pass
    return now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")


# ============================================================
# Robust GET JSON
# ============================================================
def get_json(url, params=None, timeout=20, retries=6, backoff=1.6):
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
            text = (r.text or "").strip()

            if r.status_code != 200:
                last_err = f"HTTP {r.status_code} | {url} | first120={text[:120]}"
                time.sleep(backoff * (i + 1))
                continue

            if not text:
                last_err = f"Empty response | {url}"
                time.sleep(backoff * (i + 1))
                continue

            return r.json()

        except ValueError as e:
            last_err = f"JSON decode failed | {url} | {str(e)}"
            time.sleep(backoff * (i + 1))
            continue
        except requests.RequestException as e:
            last_err = f"Request failed | {url} | {str(e)}"
            time.sleep(backoff * (i + 1))
            continue

    print(f"❌ get_json failed after {retries} retries. {last_err}")
    return None


# ============================================================
# 1) Futures symbols (USDT-M PERP)
# ============================================================
def get_futures_symbols_usdtm():
    url = f"{FUTURES_BASE}/fapi/v1/exchangeInfo"
    data = get_json(url, timeout=25, retries=7, backoff=1.7)
    if not data:
        print("❌ exchangeInfo alınamadı (proxy/HTTP/JSON sorunu).")
        return []

    out = []
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("baseAsset") in STABLECOINS:
            continue
        out.append(s["symbol"])

    print(f"✅ {len(out)} USDT-M PERP sembol bulundu.")
    return out


# ============================================================
# 2) Futures klines
# ============================================================
def get_futures_klines(symbol, interval=INTERVAL, limit=LIMIT):
    url = f"{FUTURES_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = get_json(url, params=params, timeout=25, retries=5, backoff=1.4)

    if not data or isinstance(data, dict):
        return None

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_base_volume", "taker_quote_volume", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ============================================================
# 3) Indicators
# ============================================================
def compute_indicators(df):
    df = df.copy()

    df["ema9"] = EMAIndicator(df["close"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], 21).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()
    df["ema200"] = EMAIndicator(df["close"], 200).ema_indicator()

    df["rsi"] = RSIIndicator(df["close"], 14).rsi()
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], 14).adx()

    macd = MACD(df["close"], 26, 12, 9)
    df["macd_line"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["sma20"] = SMAIndicator(df["close"], 20).sma_indicator()
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()
    df["atr_pct"] = df["atr14"] / df["close"] * 100

    df["cmf"] = ChaikinMoneyFlowIndicator(df["high"], df["low"], df["close"], df["volume"], 20).chaikin_money_flow()

    bb = BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_high"] - df["bb_low"]) / df["close"]

    df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    df["obv_slope"] = df["obv"].diff()

    # Breakout levels
    # shifting to avoid "using current candle"
    df["hh"] = df["high"].rolling(BR_LOOKBACK).max().shift(1)
    df["ll"] = df["low"].rolling(BR_LOOKBACK).min().shift(1)

    return df


# ============================================================
# 4) Common: ATR levels
# ============================================================
def calc_levels(last, side):
    entry = float(last["close"])
    atr = float(last["atr14"]) if not pd.isna(last["atr14"]) else None
    if atr is None or atr <= 0:
        return entry, None, None, None, None, None

    if side == "LONG":
        stop = entry - ATR_STOP_MULT * atr
        tp = entry + ATR_TP_MULT * atr
        stop_pct = (entry - stop) / entry * 100
        tp_pct = (tp - entry) / entry * 100
    else:
        stop = entry + ATR_STOP_MULT * atr
        tp = entry - ATR_TP_MULT * atr
        stop_pct = (stop - entry) / entry * 100
        tp_pct = (entry - tp) / entry * 100

    rr = (tp_pct / stop_pct) if stop_pct and stop_pct > 0 else None
    return entry, stop, tp, stop_pct, tp_pct, rr


def squeeze_ok(last, max_bb_width, max_atr_pct):
    if pd.isna(last.get("bb_width")) or pd.isna(last.get("atr_pct")):
        return False
    return (last["bb_width"] <= max_bb_width) and (last["atr_pct"] <= max_atr_pct)


# ============================================================
# 5) Strategy A: Trend/Momentum (OPT)
# ============================================================
def tm_candidates(last):
    # side filter + squeeze
    if TM_REQUIRE_SQUEEZE and not squeeze_ok(last, TM_MAX_BB_WIDTH, TM_MAX_ATR_PCT):
        return []

    # basic sanity
    req = ["ema9","ema21","ema50","ema200","rsi","adx","macd_line","macd_signal","macd_hist","vol_sma20","cmf"]
    if any(pd.isna(last.get(k)) for k in req):
        return []

    candidates = []

    # LONG
    if SIDE_FILTER in ("ALL", "LONG"):
        if (
            last["ema21"] > last["ema50"] * 0.995
            and last["ema9"] > last["ema21"] * 0.995
            and TM_RSI_LONG_MIN <= last["rsi"] <= TM_RSI_LONG_MAX
            and last["adx"] >= max(MIN_ADX, 18)          # trend daha güçlü olsun
            and last["macd_hist"] > 0                    # histogram pozitif
            and last["macd_line"] > last["macd_signal"]  # momentum
            and last["volume"] >= TM_VOL_MIN_RATIO * (last["vol_sma20"] or 1)
            and last["cmf"] >= TM_CMF_LONG_MIN
        ):
            candidates.append("LONG")

    # SHORT
    if SIDE_FILTER in ("ALL", "SHORT"):
        if (
            last["ema21"] < last["ema50"] * 1.005
            and last["ema9"] < last["ema21"] * 1.005
            and TM_RSI_SHORT_MIN <= last["rsi"] <= TM_RSI_SHORT_MAX
            and last["adx"] >= max(MIN_ADX, 18)
            and last["macd_hist"] < 0
            and last["macd_line"] < last["macd_signal"]
            and last["volume"] >= TM_VOL_MIN_RATIO * (last["vol_sma20"] or 1)
            and last["cmf"] <= TM_CMF_SHORT_MAX
        ):
            candidates.append("SHORT")

    return candidates


def tm_score(last, side):
    score = 0
    badges = []

    # regime alignment
    if side == "LONG" and last["ema50"] > last["ema200"]:
        score += 3; badges.append("⭐ TrendUp(50>200)")
    if side == "SHORT" and last["ema50"] < last["ema200"]:
        score += 3; badges.append("⭐ TrendDn(50<200)")

    # squeeze bonus
    if squeeze_ok(last, TM_MAX_BB_WIDTH, TM_MAX_ATR_PCT):
        score += 2; badges.append("⭐ Squeeze")

    # OBV direction
    if side == "LONG" and last["obv_slope"] > 0:
        score += 2; badges.append("⭐ OBV↑")
    if side == "SHORT" and last["obv_slope"] < 0:
        score += 2; badges.append("⭐ OBV↓")

    # ADX strength
    if last["adx"] >= 25:
        score += 2; badges.append("• ADX25+")
    elif last["adx"] >= 20:
        score += 1; badges.append("• ADX20+")

    # Volume spike
    if last["volume"] > (last["vol_sma20"] or 1) * 1.3:
        score += 1; badges.append("• VolSpike")

    # CMF direction
    if side == "LONG" and last["cmf"] > 0:
        score += 1; badges.append("• CMF+")
    if side == "SHORT" and last["cmf"] < 0:
        score += 1; badges.append("• CMF-")

    return score, ", ".join(badges)


# ============================================================
# 6) Strategy B: Breakout + Retest
# ============================================================
def br_candidates(last):
    if BR_REQUIRE_SQUEEZE and not squeeze_ok(last, BR_MAX_BB_WIDTH, BR_MAX_ATR_PCT):
        return []

    req = ["close","high","low","hh","ll","atr14","adx","vol_sma20","volume","cmf","macd_hist"]
    if any(pd.isna(last.get(k)) for k in req):
        return []

    atr = float(last["atr14"]) if last["atr14"] and last["atr14"] > 0 else None
    if atr is None:
        return []

    tol = BR_RETEST_ATR * atr  # retest tolerance

    candidates = []

    # LONG breakout+retest:
    # - breakout happened recently is hard to detect w/o prev bar states; we approximate:
    #   close >= hh (breakout) OR close is near hh (retest zone) AND candle holds above hh - tol
    if SIDE_FILTER in ("ALL", "LONG"):
        level = float(last["hh"])
        close = float(last["close"])
        low = float(last["low"])
        # retest: price came near level and held
        retest_ok = (abs(close - level) <= tol) or (low <= level + tol and close >= level - tol)
        hold_ok = close >= level - tol
        vol_ok = last["volume"] >= 0.9 * (last["vol_sma20"] or 1)
        if retest_ok and hold_ok and vol_ok and last["macd_hist"] >= 0:
            candidates.append("LONG")

    # SHORT breakout+retest:
    if SIDE_FILTER in ("ALL", "SHORT"):
        level = float(last["ll"])
        close = float(last["close"])
        high = float(last["high"])
        retest_ok = (abs(close - level) <= tol) or (high >= level - tol and close <= level + tol)
        hold_ok = close <= level + tol
        vol_ok = last["volume"] >= 0.9 * (last["vol_sma20"] or 1)
        if retest_ok and hold_ok and vol_ok and last["macd_hist"] <= 0:
            candidates.append("SHORT")

    return candidates


def br_score(last, side):
    score = 0
    badges = []

    # squeeze is mandatory/bonus
    if squeeze_ok(last, BR_MAX_BB_WIDTH, BR_MAX_ATR_PCT):
        score += 3; badges.append("⭐ Squeeze")

    # ADX should not be too low (avoid dead markets)
    if last["adx"] >= 20:
        score += 2; badges.append("⭐ ADX20+")
    elif last["adx"] >= 15:
        score += 1; badges.append("• ADX15+")

    # trend alignment helps
    if side == "LONG" and last["ema50"] > last["ema200"]:
        score += 1; badges.append("• BiasUp")
    if side == "SHORT" and last["ema50"] < last["ema200"]:
        score += 1; badges.append("• BiasDn")

    # volume
    if last["volume"] > (last["vol_sma20"] or 1) * 1.2:
        score += 2; badges.append("⭐ VolPush")
    elif last["volume"] >= 0.9 * (last["vol_sma20"] or 1):
        score += 1; badges.append("• VolOK")

    # CMF confirmation
    if side == "LONG" and last["cmf"] > 0:
        score += 1; badges.append("• CMF+")
    if side == "SHORT" and last["cmf"] < 0:
        score += 1; badges.append("• CMF-")

    return score, ", ".join(badges)


# ============================================================
# 7) HTML (two tables)
# ============================================================
def color_rr(v):
    if v is None:
        return ""
    if v >= 1.7:
        return "background:#064e3b;"
    if 1.2 <= v < 1.7:
        return "background:#78350f;"
    return "background:#7f1d1d;"


def fmt_num(x, d=4):
    if x is None:
        return "-"
    try:
        if isinstance(x, float) and math.isnan(x):
            return "-"
    except Exception:
        pass
    return f"{x:.{d}f}"


def render_table(rows, table_title):
    h = []
    h.append(f"<h2 style='margin-top:26px;color:#e2e8f0;'>{table_title} <span style='color:#94a3b8;font-size:12px;'>(rows={len(rows)})</span></h2>")
    h.append("<table><tr>")
    h.append("<th style='text-align:left'>Symbol</th><th>Side</th><th>Score</th><th>Last</th>")
    h.append("<th>Entry</th><th>Stop</th><th>TP</th><th>Stop%</th><th>TP%</th><th>R/R</th>")
    h.append("<th>RSI</th><th>ADX</th><th>ATR%</th><th>Vol xAvg</th><th>CMF</th><th style='text-align:left'>Badges</th>")
    h.append("</tr>")

    for r in rows:
        rr_style = color_rr(r["rr"])
        h.append(
            "<tr>"
            f"<td class='sym'>{r['symbol']}</td>"
            f"<td class='side'>{r['side']}</td>"
            f"<td>{r['score']}</td>"
            f"<td>{fmt_num(r['close'])}</td>"
            f"<td>{fmt_num(r['entry'])}</td>"
            f"<td>{fmt_num(r['stop'])}</td>"
            f"<td>{fmt_num(r['tp'])}</td>"
            f"<td>{fmt_num(r['stop_pct'],2)}</td>"
            f"<td>{fmt_num(r['tp_pct'],2)}</td>"
            f"<td style='{rr_style}'>{fmt_num(r['rr'],2)}</td>"
            f"<td>{fmt_num(r['rsi'],2)}</td>"
            f"<td>{fmt_num(r['adx'],2)}</td>"
            f"<td>{fmt_num(r['atr_pct'],2)}</td>"
            f"<td>{fmt_num(r['vol_ratio'],2)}</td>"
            f"<td>{fmt_num(r['cmf'],3)}</td>"
            f"<td style='text-align:left'>{r['badges']}</td>"
            "</tr>"
        )

    h.append("</table>")
    return "\n".join(h)


def generate_page(rows_tm, rows_br, output_path, title, meta_top):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    h = [
        "<html><head><meta charset='UTF-8'>",
        f"<title>{title}</title>",
        "<style>",
        "body{background:#020617;color:#e5e7eb;font-family:Arial;padding:20px;}",
        "table{width:100%;border-collapse:collapse;margin-top:10px;}",
        "th{background:#111827;padding:6px;font-size:12px;text-align:right;position:sticky;top:0;}",
        "td{padding:6px;font-size:12px;text-align:right;}",
        "tr:nth-child(even){background:#0f172a;}",
        "tr:nth-child(odd){background:#1f2937;}",
        ".sym{text-align:left;font-weight:bold;}",
        ".side{text-align:center;font-weight:bold;}",
        ".badge{color:#94a3b8;font-size:12px;}",
        "</style></head><body>",
        f"<h1 style='text-align:center;color:#facc15;'>{title}</h1>",
        f"<div style='text-align:center;color:#94a3b8;'>{meta_top}</div>",
        "<div style='text-align:center;color:#64748b;margin-top:6px;font-size:12px;'>"
        "Not: Bu çıktılar finansal tavsiye değildir. Kaldıraçta risk yönetimi şarttır.</div>",
    ]

    h.append(render_table(rows_tm, "1) Trend/Momentum (OPT) – daha seçici sinyaller"))
    h.append(render_table(rows_br, "2) Breakout + Retest – sıkışma sonrası retest onayı"))

    h.append("</body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


# ============================================================
# 8) MAIN
# ============================================================
def main():
    updated_at = get_updated_at_str()

    symbols = get_futures_symbols_usdtm()
    total_symbols = len(symbols)

    if not symbols:
        title = "Futures Dashboard (NO DATA - proxy blocked?)"
        meta = f"Güncellendi: {updated_at} | interval={INTERVAL} | symbols=0 | TM_rows=0 | BR_rows=0"
        generate_page([], [], "public/spot.html", title, meta)
        generate_page([], [], "public/futures_ls.html", title, meta)
        print("⚠️ Sembol listesi boş. Boş dashboard basıldı.")
        return

    rows_tm = []
    rows_br = []

    for sym in symbols:
        try:
            df = get_futures_klines(sym, INTERVAL, LIMIT)
            if df is None or len(df) < max(220, BR_LOOKBACK + 20):
                continue

            df = compute_indicators(df)
            last = df.iloc[-1]

            # shared fields
            vol_ratio = float(last["volume"]) / float((last["vol_sma20"] or 1))

            # ---------- Strategy A: Trend/Momentum OPT ----------
            cands_tm = tm_candidates(last)
            for side in cands_tm:
                entry, stop, tp, stop_pct, tp_pct, rr = calc_levels(last, side)
                if rr is None or rr < MIN_RR:
                    continue
                score, badges = tm_score(last, side)
                if score < TM_MIN_SCORE:
                    continue
                rows_tm.append({
                    "symbol": sym,
                    "side": side,
                    "close": float(last["close"]),
                    "score": int(score),
                    "entry": entry,
                    "stop": stop,
                    "tp": tp,
                    "stop_pct": stop_pct,
                    "tp_pct": tp_pct,
                    "rr": rr,
                    "rsi": float(last["rsi"]) if not pd.isna(last["rsi"]) else None,
                    "adx": float(last["adx"]) if not pd.isna(last["adx"]) else None,
                    "atr_pct": float(last["atr_pct"]) if not pd.isna(last["atr_pct"]) else None,
                    "vol_ratio": vol_ratio,
                    "cmf": float(last["cmf"]) if not pd.isna(last["cmf"]) else None,
                    "badges": badges
                })

            # ---------- Strategy B: Breakout+Retest ----------
            cands_br = br_candidates(last)
            for side in cands_br:
                entry, stop, tp, stop_pct, tp_pct, rr = calc_levels(last, side)
                if rr is None or rr < MIN_RR:
                    continue
                score, badges = br_score(last, side)
                if score < BR_MIN_SCORE:
                    continue
                rows_br.append({
                    "symbol": sym,
                    "side": side,
                    "close": float(last["close"]),
                    "score": int(score),
                    "entry": entry,
                    "stop": stop,
                    "tp": tp,
                    "stop_pct": stop_pct,
                    "tp_pct": tp_pct,
                    "rr": rr,
                    "rsi": float(last["rsi"]) if not pd.isna(last["rsi"]) else None,
                    "adx": float(last["adx"]) if not pd.isna(last["adx"]) else None,
                    "atr_pct": float(last["atr_pct"]) if not pd.isna(last["atr_pct"]) else None,
                    "vol_ratio": vol_ratio,
                    "cmf": float(last["cmf"]) if not pd.isna(last["cmf"]) else None,
                    "badges": badges
                })

            time.sleep(SLEEP)
        except Exception:
            continue

    # sort & trim
    rows_tm_sorted = sorted(rows_tm, key=lambda r: (-r["score"], -(r["rr"] or 0)))[:TM_MAX_ROWS]
    rows_br_sorted = sorted(rows_br, key=lambda r: (-r["score"], -(r["rr"] or 0)))[:BR_MAX_ROWS]

    title = "Binance Futures (USDT-M PERP) – 2 Strateji (Long/Short)"
    meta = (
        f"Güncellendi: {updated_at} | interval={INTERVAL} | symbols={total_symbols} "
        f"| TM_rows={len(rows_tm_sorted)} | BR_rows={len(rows_br_sorted)}"
    )

    os.makedirs("public", exist_ok=True)
    # tek sayfa: spot.html (index yönlendirmesi bozulmasın)
    generate_page(rows_tm_sorted, rows_br_sorted, "public/spot.html", title, meta)
    # istersen ayrıca futures_ls.html de aynı sayfa olsun
    generate_page(rows_tm_sorted, rows_br_sorted, "public/futures_ls.html", title, meta)

    print(f"✅ Dashboard üretildi: TM_rows={len(rows_tm_sorted)} | BR_rows={len(rows_br_sorted)}")


if __name__ == "__main__":
    main()
