import os
import time
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD, SMAIndicator
from ta.momentum import RSIIndicator, ROCIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import ChaikinMoneyFlowIndicator, OnBalanceVolumeIndicator

BINANCE_SPOT_BASE = "https://binance-proxy-63js.onrender.com/api"

# ----------------------------------------------------------
# 1) Sembol Listesi
# ----------------------------------------------------------
def get_spot_symbols():
    url = f"{BINANCE_SPOT_BASE}/api/v3/exchangeInfo"
    data = requests.get(url, timeout=15).json()
    symbols = [
        s["symbol"]
        for s in data.get("symbols", [])
        if s.get("status") == "TRADING"
        and s.get("quoteAsset") == "USDT"
        and s.get("isSpotTradingAllowed", False)
    ]
    print(f"✅ {len(symbols)} spot sembol bulundu.")
    return symbols


# ----------------------------------------------------------
# 2) Kline
# ----------------------------------------------------------
def get_klines(symbol, interval="1d", limit=120):
    url = f"{BINANCE_SPOT_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = requests.get(url, params=params, timeout=10).json()
    if isinstance(data, dict):
        return None

    df = pd.DataFrame(data, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_base_volume","taker_quote_volume","ignore"
    ])

    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)

    return df


# ----------------------------------------------------------
# 3) İndikatörler
# ----------------------------------------------------------
def compute_indicators(df):
    df = df.copy()

    # EMAs
    df["ema9"] = EMAIndicator(df["close"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], 21).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()
    df["ema200"] = EMAIndicator(df["close"], 200).ema_indicator()

    # RSI
    df["rsi"] = RSIIndicator(df["close"], 14).rsi()

    # ADX
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], 14).adx()

    # MACD
    macd = MACD(df["close"], 26, 12, 9)
    df["macd_line"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # SMA20
    df["sma20"] = SMAIndicator(df["close"], 20).sma_indicator()

    # Volume avg
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    # ATR
    df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()

    # CMF
    df["cmf"] = ChaikinMoneyFlowIndicator(df["high"], df["low"], df["close"], df["volume"], 20).chaikin_money_flow()

    # OBV
    df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    df["obv_slope"] = df["obv"].diff()

    # Bollinger Bands
    bb = BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_high"] - df["bb_low"]) / df["close"]

    return df


# ----------------------------------------------------------
# 4) Soft Daily / Weekly Filtreler (senin eski ayar)
# ----------------------------------------------------------
def daily_long_condition(last):
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","vol_sma20"]):
        return False
    return (
        last["ema9"] > last["ema21"] * 0.995
        and 42 < last["rsi"] < 68
        and last["adx"] > 15
        and last["macd_line"] > last["macd_signal"]
        and last["volume"] > 0.9 * (last["vol_sma20"] or 1)
        and last.get("cmf", 0) > -0.05
    )

def weekly_long_condition(last):
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","rsi","adx","macd_line","macd_signal"]):
        return False
    return (
        last["ema9"] > last["ema21"]
        and last["rsi"] > 48
        and last["adx"] > 18
        and last["macd_line"] > last["macd_signal"]
    )


# ----------------------------------------------------------
# 5) BullScore
# ----------------------------------------------------------
def compute_bull_score(last):
    score = 0

    # Golden Cross
    if last.get("ema50") and last.get("ema200") and last["ema50"] > last["ema200"]:
        score += 3

    # Sıkışma
    if last.get("bb_width") and last["bb_width"] < 0.06 and last.get("atr14") and last["atr14"] / last["close"] < 0.03:
        score += 2

    # MACD uptrend
    if last["macd_line"] > last["macd_signal"]:
        score += 1

    # RSI zone
    if 45 < last["rsi"] < 60:
        score += 1

    # CMF pozitif
    if last.get("cmf") and last["cmf"] > 0:
        score += 1

    # OBV
    if last.get("obv_slope") and last["obv_slope"] > 0:
        score += 2

    # Hacim spike
    if last["volume"] > (last["vol_sma20"] or 1) * 1.3:
        score += 1

    # Trend bounce
    if last["close"] > last["ema50"] and abs(last["close"] - last["sma20"]) / last["close"] < 0.02:
        score += 1

    return score


# ----------------------------------------------------------
# 6) HTML – score.html
# ----------------------------------------------------------
def generate_score_html(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    h = [
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>",
        "<title>BullScore Report</title>",
        "<style>body{font-family:Arial;background:#020617;color:#e5e7eb;padding:20px;}"
        "table{width:100%;border-collapse:collapse;margin-top:20px;}"
        "th,td{padding:6px;font-size:12px;text-align:right;}th{background:#1e293b;}"
        "tr:nth-child(even){background:#0f172a;}tr:nth-child(odd){background:#111827;}"
        ".sym{text-align:left;font-weight:bold;color:#facc15;}</style></head><body>",
        "<h1 style='text-align:center;color:#facc15'>BullScore - Çıkış Eğilimi Skoru</h1>",
        "<p style='text-align:center;color:#9ca3af;font-size:12px'>Yatırım tavsiyesi değildir</p>",
        "<table><tr><th style='text-align:left'>Symbol</th><th>Last</th><th>Score</th><th>RSI</th><th>MACD</th><th>CMF</th><th>ATR%</th></tr>"
    ]

    for r in rows:
        fmt = lambda x, d=4: f"{x:.{d}f}" if x is not None else "-"
        h.append(
            f"<tr><td class='sym'>{r['symbol']}</td>"
            f"<td>{fmt(r['close'])}</td>"
            f"<td style='font-weight:bold;color:#fcd34d'>{r['score']}</td>"
            f"<td>{fmt(r['rsi'],2)}</td>"
            f"<td>{fmt(r['macd'],3)}</td>"
            f"<td>{fmt(r['cmf'],3)}</td>"
            f"<td>{fmt(r['atr_pct'],3)}</td></tr>"
        )

    h.append("</table></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


# ----------------------------------------------------------
# 7) Ana Akış
# ----------------------------------------------------------
def main():
    symbols = get_spot_symbols()
    rows_spot = []
    rows_score = []

    for sym in symbols:
        try:
            df_d = get_klines(sym, "1d", 120)
            df_w = get_klines(sym, "1w", 120)

            if df_d is None or len(df_d) < 60:
                continue

            df_d = compute_indicators(df_d)
            last = df_d.iloc[-1]

            # Spot raporu
            daily = daily_long_condition(last)
            weekly = weekly_long_condition(last)

            rows_spot.append({
                "symbol": sym,
                "last_price": last["close"],
                "daily_long": daily,
                "weekly_long": weekly,
                "rsi": last["rsi"],
                "adx": last["adx"],
                "vol_ratio": last["volume"] / (last["vol_sma20"] or 1),
                "cmf": last["cmf"],
            })

            # Score raporu
            score = compute_bull_score(last)
            rows_score.append({
                "symbol": sym,
                "close": last["close"],
                "score": score,
                "rsi": last["rsi"],
                "macd": last["macd_hist"],
                "cmf": last["cmf"],
                "atr_pct": last["atr14"] / last["close"],
            })

            time.sleep(0.07)
        except:
            continue

    # Spot için eski fonksiyon (senin spot.html'ini çağıracağız)
    rows_spot_sorted = sorted(rows_spot, key=lambda r: (not r["daily_long"], not r["weekly_long"], -r["adx"]))
    from main_spot_html_generator import generate_html_report  # Eğer ayrı dosyadaysa
    generate_html_report(rows_spot_sorted, "public/spot.html")

    # Score raporu
    rows_score_sorted = sorted(rows_score, key=lambda r: -r["score"])
    generate_score_html(rows_score_sorted, "public/score.html")

    print("✅ spot.html ve score.html üretildi")


if __name__ == "__main__":
    main()
