import os
import time
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import ChaikinMoneyFlowIndicator, OnBalanceVolumeIndicator

BINANCE_SPOT_BASE = "https://binance-proxy-63js.onrender.com/api"

# ============================================================
# 1) Spot Sembol Listesi
# ============================================================
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


# ============================================================
# 2) Kline Verisi
# ============================================================
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
    for col in ["open","high","low","close","volume"]:
        df[col] = df[col].astype(float)
    return df


# ============================================================
# 3) Göstergeler
# ============================================================
def compute_indicators(df):
    df = df.copy()

    # EMAs
    df["ema9"] = EMAIndicator(df["close"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], 21).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()
    df["ema200"] = EMAIndicator(df["close"], 200).ema_indicator()

    # RSI / ADX
    df["rsi"] = RSIIndicator(df["close"], 14).rsi()
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], 14).adx()

    # MACD
    macd = MACD(df["close"], 26, 12, 9)
    df["macd_line"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # SMA20 ve hacim ortalaması
    df["sma20"] = SMAIndicator(df["close"], 20).sma_indicator()
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    # ATR
    df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()
    df["atr_pct"] = df["atr14"] / df["close"]

    # CMF
    df["cmf"] = ChaikinMoneyFlowIndicator(
        df["high"], df["low"], df["close"], df["volume"], 20
    ).chaikin_money_flow()

    # Bollinger Bandları
    bb = BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_high"] - df["bb_low"]) / df["close"]

    # OBV
    df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    df["obv_slope"] = df["obv"].diff()

    return df


# ============================================================
# 4) Günlük Trend Filtreleri (eski "Daily BUY" mantığı)
# ============================================================
def daily_long_condition(last):
    required = ["ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","vol_sma20","cmf"]
    if any(pd.isna(last.get(k)) for k in required):
        return False

    return (
        last["ema9"] > last["ema21"] * 0.995
        and 42 < last["rsi"] < 68
        and last["adx"] > 15
        and last["macd_line"] > last["macd_signal"]
        and last["volume"] > 0.9 * (last["vol_sma20"] or 1)
        and last["cmf"] > -0.05
    )


# ============================================================
# 5) BullScore & Strateji Etiketleri
# ============================================================
def compute_bull_score(last):
    score = 0
    strategies = []

    # 1) Golden Cross
    if last["ema50"] > last["ema200"]:
        score += 3
        strategies.append("Golden Cross")

    # 2) Sıkışma (BB dar + ATR düşük)
    if last["bb_width"] < 0.06 and last["atr_pct"] < 0.03:
        score += 2
        strategies.append("Sıkışma (BB+ATR)")

    # 3) MACD Momentum
    if last["macd_line"] > last["macd_signal"]:
        score += 1
        strategies.append("MACD Uptrend")

    # 4) RSI orta bölge
    if 45 < last["rsi"] < 60:
        score += 1
        strategies.append("RSI Middle Zone")

    # 5) CMF pozitif para akışı
    if last["cmf"] > 0:
        score += 1
        strategies.append("CMF +")

    # 6) OBV yukarı
    if last["obv_slope"] > 0:
        score += 2
        strategies.append("OBV ↑")

    # 7) Hacim spike
    if last["volume"] > (last["vol_sma20"] or 1) * 1.3:
        score += 1
        strategies.append("Volume Spike")

    # 8) Trend bounce (EMA50 üstü + SMA20 civarı)
    if last["close"] > last["ema50"] and abs(last["close"] - last["sma20"]) / last["close"] < 0.02:
        score += 1
        strategies.append("Trend Bounce")

    return score, ", ".join(strategies)


# ============================================================
# 6) Entry / Stop / Take Profit (ATR tabanlı)
# ============================================================
def calc_entry_stop_tp(last, atr_mult_stop=1.5, atr_mult_tp=2.5):
    price = last["close"]
    atr = last.get("atr14")
    if pd.isna(atr) or atr <= 0:
        # ATR yoksa en azından entry döndür
        return price, None, None

    entry = price
    stop = max(price - atr_mult_stop * atr, 0)
    tp = price + atr_mult_tp * atr

    return entry, stop, tp


# ============================================================
# 7) Ultra Dashboard HTML (spot.html)
# ============================================================
def generate_dashboard_html(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    h = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>",
        "<title>Binance Spot Dashboard</title>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        "<style>",
        "body{font-family:Arial, sans-serif; background:#020617; color:#e5e7eb; padding:20px;}",
        "h1{ text-align:center; color:#facc15; margin-bottom:8px; }",
        "p.subtitle{ text-align:center; color:#9ca3af; font-size:12px; margin-top:0; }",
        "table{width:100%; border-collapse:collapse; margin-top:20px;}",
        "th,td{padding:6px; font-size:11px; text-align:right;}",
        "th{background:#111827; position:sticky; top:0; z-index:1;}",
        "tr:nth-child(even){background:#0f172a;}",
        "tr:nth-child(odd){background:#1f2937;}",
        ".sym{text-align:left; font-weight:bold;}",
        ".score-high{color:#fcd34d; font-weight:bold;}",
        ".buy{color:#22c55e; font-weight:bold;}",
        ".nowrap{white-space:nowrap;}",
        "</style></head><body>",
        "<h1>Binance Spot – Ultra Dashboard</h1>",
        "<p class='subtitle'>BullScore, günlük trend, ATR tabanlı entry/stop/TP ve temel indikatörler tek tabloda. Yatırım tavsiyesi değildir.</p>",
        "<table>",
        "<tr>",
        "<th style='text-align:left'>Symbol</th>",
        "<th>Score</th>",
        "<th>Last</th>",
        "<th>Daily</th>",
        "<th>Entry</th>",
        "<th>Stop</th>",
        "<th>TP</th>",
        "<th>RSI</th>",
        "<th>ADX</th>",
        "<th>Vol xAvg</th>",
        "<th>CMF</th>",
        "<th>ATR%</th>",
        "<th style='text-align:left'>Stratejiler</th>",
        "</tr>"
    ]

    for r in rows:
        fmt = lambda x, d=4: f"{x:.{d}f}" if x is not None else "-"
        score_cls = "score-high" if r["score"] >= 6 else ""
        daily_txt = "BUY" if r["daily_long"] else "-"
        daily_cls = "buy" if r["daily_long"] else ""

        h.append(
            "<tr>"
            f"<td class='sym'>{r['symbol']}</td>"
            f"<td class='{score_cls}'>{r['score']}</td>"
            f"<td>{fmt(r['close'])}</td>"
            f"<td class='{daily_cls}'>{daily_txt}</td>"
            f"<td>{fmt(r['entry'])}</td>"
            f"<td>{fmt(r['stop'])}</td>"
            f"<td>{fmt(r['tp'])}</td>"
            f"<td>{fmt(r['rsi'],2)}</td>"
            f"<td>{fmt(r['adx'],2)}</td>"
            f"<td>{fmt(r['vol_ratio'],2)}</td>"
            f"<td>{fmt(r['cmf'],3)}</td>"
            f"<td>{fmt(r['atr_pct'],3)}</td>"
            f"<td style='text-align:left' class='nowrap'>{r['strategies']}</td>"
            "</tr>"
        )

    h.append("</table></body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


# ============================================================
# 8) Ana Akış
# ============================================================
def main():
    symbols = get_spot_symbols()
    rows = []

    for sym in symbols:
        try:
            df = get_klines(sym, "1d", 120)
            if df is None or len(df) < 60:
                continue

            df = compute_indicators(df)
            last = df.iloc[-1]

            daily = daily_long_condition(last)
            vol_ratio = last["volume"] / (last["vol_sma20"] or 1)
            score, strategies = compute_bull_score(last)
            entry, stop, tp = calc_entry_stop_tp(last)

            rows.append({
                "symbol": sym,
                "close": last["close"],
                "score": score,
                "daily_long": daily,
                "entry": entry,
                "stop": stop,
                "tp": tp,
                "rsi": last["rsi"],
                "adx": last["adx"],
                "vol_ratio": vol_ratio,
                "cmf": last["cmf"],
                "atr_pct": last["atr_pct"],
                "strategies": strategies,
            })

            time.sleep(0.05)
        except Exception:
            continue

    # Skora göre, sonra Daily BUY, sonra ADX’e göre sırala
    rows_sorted = sorted(
        rows,
        key=lambda r: (-r["score"], -int(r["daily_long"]), -r["adx"])
    )

    os.makedirs("public", exist_ok=True)
    generate_dashboard_html(rows_sorted, "public/spot.html")
    print("✅ spot.html (Ultra Dashboard) üretildi")


if __name__ == "__main__":
    main()
