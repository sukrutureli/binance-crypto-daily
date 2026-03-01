import os
import time
import math
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import ChaikinMoneyFlowIndicator, OnBalanceVolumeIndicator


# ============================================================
# CONFIG
# ============================================================
# ÖNEMLİ: Senin proxy SPOT'u /api/v3 ile çalıştırıyor.
# FUTURES için de /api/fapi/... prefix kullanacağız.
PROXY_BASE = "https://binance-proxy-63js.onrender.com/api"
FUTURES_BASE = PROXY_BASE  # -> /api/fapi/v1/...

INTERVAL = os.getenv("INTERVAL", "1h")         # 15m / 1h / 4h / 1d
LIMIT = int(os.getenv("LIMIT", "300"))         # 200+ önerilir
SLEEP = float(os.getenv("SLEEP", "0.02"))

ONLY_SIGNAL = os.getenv("ONLY_SIGNAL", "1") == "1"
SIDE_FILTER = os.getenv("SIDE", "ALL").upper()       # ALL / LONG / SHORT
MIN_RR = float(os.getenv("MIN_RR", "1.2"))
MIN_ADX = float(os.getenv("MIN_ADX", "15"))
ATR_STOP_MULT = float(os.getenv("ATR_STOP", "1.5"))
ATR_TP_MULT = float(os.getenv("ATR_TP", "2.5"))

STABLECOINS = {"USDT", "BUSD", "DAI", "USDC", "TUSD", "FDUSD"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CryptoDailyBot/1.0)",
    "Accept": "application/json,text/plain,*/*",
}


# ============================================================
# Robust GET JSON (proxy bazen HTML/boş/403 döndürebilir)
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
# 1) Futures Sembol Listesi (USDT-M perpetual)
# ============================================================
def get_futures_symbols_usdtm():
    # DİKKAT: /api + /fapi/v1/exchangeInfo => /api/fapi/v1/exchangeInfo
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
# 2) Kline Verisi (Futures)
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

    return df


# ============================================================
# 4) Long/Short conditions
# ============================================================
def long_condition(last):
    req = ["ema9", "ema21", "ema50", "rsi", "adx", "macd_line", "macd_signal", "vol_sma20", "cmf"]
    if any(pd.isna(last.get(k)) for k in req):
        return False
    return (
        last["ema9"] > last["ema21"] * 0.995
        and last["ema21"] > last["ema50"] * 0.995
        and 42 < last["rsi"] < 68
        and last["adx"] >= MIN_ADX
        and last["macd_line"] > last["macd_signal"]
        and last["volume"] > 0.8 * (last["vol_sma20"] or 1)
        and last["cmf"] > -0.10
    )


def short_condition(last):
    req = ["ema9", "ema21", "ema50", "rsi", "adx", "macd_line", "macd_signal", "vol_sma20", "cmf"]
    if any(pd.isna(last.get(k)) for k in req):
        return False
    return (
        last["ema9"] < last["ema21"] * 1.005
        and last["ema21"] < last["ema50"] * 1.005
        and 32 < last["rsi"] < 58
        and last["adx"] >= MIN_ADX
        and last["macd_line"] < last["macd_signal"]
        and last["volume"] > 0.8 * (last["vol_sma20"] or 1)
        and last["cmf"] < 0.10
    )


# ============================================================
# 5) Score + badges
# ============================================================
def compute_score_and_badges(last, side):
    score = 0
    strong, moderate = [], []

    if side == "LONG":
        if last["ema50"] > last["ema200"]:
            score += 3; strong.append("Golden Cross")
        if last["obv_slope"] > 0:
            score += 2; strong.append("OBV ↑")
    else:
        if last["ema50"] < last["ema200"]:
            score += 3; strong.append("Death Cross")
        if last["obv_slope"] < 0:
            score += 2; strong.append("OBV ↓")

    if last["bb_width"] < 0.06 and last["atr_pct"] < 3:
        score += 2; strong.append("Sıkışma (BB+ATR)")

    if side == "LONG" and last["macd_line"] > last["macd_signal"]:
        score += 1; moderate.append("MACD Up")
    if side == "SHORT" and last["macd_line"] < last["macd_signal"]:
        score += 1; moderate.append("MACD Down")

    if side == "LONG" and 45 < last["rsi"] < 60:
        score += 1; moderate.append("RSI Zone")
    if side == "SHORT" and 40 < last["rsi"] < 55:
        score += 1; moderate.append("RSI Zone")

    if side == "LONG" and last["cmf"] > 0:
        score += 1; moderate.append("CMF+")
    if side == "SHORT" and last["cmf"] < 0:
        score += 1; moderate.append("CMF-")

    if last["volume"] > (last["vol_sma20"] or 1) * 1.3:
        score += 1; moderate.append("Volume Spike")

    badges = ", ".join(filter(None, [
        ", ".join(["⭐ " + s for s in strong]) if strong else "",
        ", ".join(["• " + s for s in moderate]) if moderate else ""
    ]))
    return score, badges


# ============================================================
# 6) Entry/Stop/TP
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


# ============================================================
# 7) HTML
# ============================================================
def color_rr(v):
    if v is None: return ""
    if v >= 1.5: return "background:#064e3b;"
    if 1.0 <= v < 1.5: return "background:#78350f;"
    return "background:#7f1d1d;"


def generate_html(rows, output_path, title):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    h = [
        "<html><head><meta charset='UTF-8'>",
        f"<title>{title}</title>",
        "<style>",
        "body{background:#020617;color:#e5e7eb;font-family:Arial;padding:20px;}",
        "table{width:100%;border-collapse:collapse;margin-top:20px;}",
        "th{background:#111827;padding:6px;font-size:12px;text-align:right;}",
        "td{padding:6px;font-size:12px;text-align:right;}",
        "tr:nth-child(even){background:#0f172a;}",
        "tr:nth-child(odd){background:#1f2937;}",
        ".sym{text-align:left;font-weight:bold;}",
        ".side{text-align:center;font-weight:bold;}",
        "</style></head><body>",
        f"<h1 style='text-align:center;color:#facc15;'>{title}</h1>",
        f"<div style='text-align:center;color:#94a3b8;'>interval={INTERVAL} | only_signal={ONLY_SIGNAL} | side={SIDE_FILTER} | minRR={MIN_RR}</div>",
        "<table><tr>",
        "<th style='text-align:left'>Symbol</th><th>Side</th><th>Score</th><th>Last</th>",
        "<th>Entry</th><th>Stop</th><th>TP</th><th>Stop%</th><th>TP%</th><th>R/R</th>",
        "<th>RSI</th><th>ADX</th><th>ATR%</th><th>Vol xAvg</th><th>CMF</th><th style='text-align:left'>Badges</th>",
        "</tr>"
    ]

    def fmt(x, d=4):
        if x is None: return "-"
        try:
            if isinstance(x, float) and math.isnan(x): return "-"
        except Exception:
            pass
        return f"{x:.{d}f}"

    for r in rows:
        rr_style = color_rr(r["rr"])
        h.append(
            "<tr>"
            f"<td class='sym'>{r['symbol']}</td>"
            f"<td class='side'>{r['side']}</td>"
            f"<td>{r['score']}</td>"
            f"<td>{fmt(r['close'])}</td>"
            f"<td>{fmt(r['entry'])}</td>"
            f"<td>{fmt(r['stop'])}</td>"
            f"<td>{fmt(r['tp'])}</td>"
            f"<td>{fmt(r['stop_pct'],2)}</td>"
            f"<td>{fmt(r['tp_pct'],2)}</td>"
            f"<td style='{rr_style}'>{fmt(r['rr'],2)}</td>"
            f"<td>{fmt(r['rsi'],2)}</td>"
            f"<td>{fmt(r['adx'],2)}</td>"
            f"<td>{fmt(r['atr_pct'],2)}</td>"
            f"<td>{fmt(r['vol_ratio'],2)}</td>"
            f"<td>{fmt(r['cmf'],3)}</td>"
            f"<td style='text-align:left'>{r['badges']}</td>"
            "</tr>"
        )

    h.append("</table></body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


# ============================================================
# 8) MAIN
# ============================================================
def main():
    symbols = get_futures_symbols_usdtm()

    if not symbols:
        title = "Futures Dashboard (NO DATA - proxy blocked?)"
        os.makedirs("public", exist_ok=True)
        generate_html([], "public/futures_ls.html", title)
        generate_html([], "public/spot.html", title)
        print("⚠️ Sembol listesi boş. Boş dashboard basıldı, çıkılıyor.")
        return

    rows = []

    for sym in symbols:
        try:
            df = get_futures_klines(sym, INTERVAL, LIMIT)
            if df is None or len(df) < 220:
                continue

            df = compute_indicators(df)
            last = df.iloc[-1]

            is_long = long_condition(last)
            is_short = short_condition(last)

            candidates = []
            if SIDE_FILTER in ("ALL", "LONG") and is_long:
                candidates.append("LONG")
            if SIDE_FILTER in ("ALL", "SHORT") and is_short:
                candidates.append("SHORT")

            if not ONLY_SIGNAL and not candidates:
                bias = "LONG" if (last["ema50"] > last["ema200"]) else "SHORT"
                candidates = [bias]

            for side in candidates:
                score, badges = compute_score_and_badges(last, side)
                entry, stop, tp, stop_pct, tp_pct, rr = calc_levels(last, side)
                if rr is None or rr < MIN_RR:
                    continue

                rows.append({
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
                    "vol_ratio": float(last["volume"]) / float((last["vol_sma20"] or 1)),
                    "cmf": float(last["cmf"]) if not pd.isna(last["cmf"]) else None,
                    "badges": badges
                })

            time.sleep(SLEEP)
        except Exception:
            continue

    rows_sorted = sorted(rows, key=lambda r: (-r["score"], -(r["rr"] or 0)))

    os.makedirs("public", exist_ok=True)
    title = "Binance Futures (USDT-M PERP) – Long/Short Dashboard"
    generate_html(rows_sorted, "public/futures_ls.html", title)
    generate_html(rows_sorted, "public/spot.html", title)  # index/workflow bozulmasın
    print(f"✅ Dashboard üretildi: rows={len(rows_sorted)}")


if __name__ == "__main__":
    main()
