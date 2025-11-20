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

    # Trend / hareketli ortalamalar
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

    # Hacim ortalaması
    df["sma20"] = SMAIndicator(df["close"], 20).sma_indicator()
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    # ATR
    df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()
    df["atr_pct"] = df["atr14"] / df["close"]

    # Para akışı
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
# 4) Günlük Trend Filtreleri (soft mode)
# ============================================================
def daily_long_condition(last):
    if any(pd.isna(last.get(k)) for k in [
        "ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","vol_sma20"
    ]):
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
        # ATR hesaplanamamışsa en azından entry dönsün
        return price, None, None

    entry = price                   # istersen Binance'de biraz altına limit yazabilirsin
    stop = max(price - atr_mult_stop * atr, 0)
    tp = price + atr_mult_tp * atr  # swing hedefi

    return entry, stop, tp


# ============================================================
# 7) spot.html – Basit günlük görünüm
# ============================================================
def generate_spot_html(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    h = [
        "<html><head><meta charset='UTF-8'>",
        "<title>Binance Spot</title>",
        "<style>body{font-family:Arial;background:#020617;color:#e5e7eb;padding:20px;}"
        "table{width:100%;border-collapse:collapse;}"
        "th{background:#111827;}tr:nth-child(even){background:#0f172a;}"
        "tr:nth-child(odd){background:#1e293b;}"
        "td,th{padding:5px;font-size:12px;text-align:right;}"
        ".sym{text-align:left;font-weight:bold;}</style>",
        "</head><body>",
        "<h1 style='text-align:center;color:#a855f7'>Binance Spot Günlük Analiz</h1>",
        "<table><tr>"
        "<th style='text-align:left'>Symbol</th>"
        "<th>Last</th><th>Daily</th><th>RSI</th><th>ADX</th><th>Vol xAvg</th><th>CMF</th>"
        "</tr>"
    ]

    for r in rows:
        fmt = lambda x, d=4: f"{x:.{d}f}" if x is not None else "-"
        h.append(
            f"<tr><td class='sym'>{r['symbol']}</td>"
            f"<td>{fmt(r['last_price'])}</td>"
            f"<td>{'BUY' if r['daily_long'] else '-'}</td>"
            f"<td>{fmt(r['rsi'],2)}</td>"
            f"<td>{fmt(r['adx'],2)}</td>"
            f"<td>{fmt(r['vol_ratio'],2)}</td>"
            f"<td>{fmt(r['cmf'],3)}</td></tr>"
        )

    h.append("</table></body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


# ============================================================
# 8) score.html – Entry / Stop / TP’li BullScore tablosu
# ============================================================
def generate_score_html(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    h = [
        "<html><head><meta charset='UTF-8'>",
        "<title>BullScore</title>",
        "<style>body{font-family:Arial;background:#020617;color:#e5e7eb;padding:20px;}"
        "table{width:100%;border-collapse:collapse;}"
        "th{background:#1e293b;}tr:nth-child(even){background:#0f172a;}"
        "tr:nth-child(odd){background:#111827;}"
        "td,th{padding:6px;font-size:12px;text-align:right;}"
        ".sym{text-align:left;font-weight:bold;color:#facc15;}</style>",
        "</head><body>",
        "<h1 style='text-align:center;color:#facc15'>BullScore – Çıkış Eğilimi Skoru</h1>",
        "<p style='text-align:center;color:#9ca3af;font-size:12px'>"
        "Entry/Stop/TP seviyeleri ATR tabanlı swing hesaplarıdır. Yatırım tavsiyesi değildir."
        "</p>",
        "<table><tr>"
        "<th style='text-align:left'>Symbol</th>"
        "<th>Last</th><th>Entry</th><th>Stop</th><th>TP</th>"
        "<th>Score</th><th>Stratejiler</th>"
        "<th>RSI</th><th>MACD</th><th>CMF</th><th>ATR%</th>"
        "</tr>"
    ]

    for r in rows:
        fmt = lambda x, d=4: f"{x:.{d}f}" if x is not None else "-"
        h.append(
            f"<tr><td class='sym'>{r['symbol']}</td>"
            f"<td>{fmt(r['close'])}</td>"
            f"<td>{fmt(r['entry'])}</td>"
            f"<td>{fmt(r['stop'])}</td>"
            f"<td>{fmt(r['tp'])}</td>"
            f"<td style='font-weight:bold;color:#fcd34d'>{r['score']}</td>"
            f"<td style='text-align:left'>{r['strategies']}</td>"
            f"<td>{fmt(r['rsi'],2)}</td>"
            f"<td>{fmt(r['macd'],3)}</td>"
            f"<td>{fmt(r['cmf'],3)}</td>"
            f"<td>{fmt(r['atr_pct'],3)}</td></tr>"
        )

    h.append("</table></body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


# ============================================================
# 9) Ana Akış
# ============================================================
def main():
    symbols = get_spot_symbols()

    spot_rows = []
    score_rows = []

    for sym in symbols:
        try:
            df = get_klines(sym, "1d", 120)
            if df is None or len(df) < 60:
                continue

            df = compute_indicators(df)
            last = df.iloc[-1]

            # Spot görünümü için
            daily = daily_long_condition(last)
            vol_ratio = last["volume"] / (last["vol_sma20"] or 1)

            spot_rows.append({
                "symbol": sym,
                "last_price": last["close"],
                "daily_long": daily,
                "rsi": last["rsi"],
                "adx": last["adx"],
                "vol_ratio": vol_ratio,
                "cmf": last["cmf"],
            })

            # BullScore + Entry/Stop/TP için
            score, strategies = compute_bull_score(last)
            entry, stop, tp = calc_entry_stop_tp(last)

            score_rows.append({
                "symbol": sym,
                "close": last["close"],
                "entry": entry,
                "stop": stop,
                "tp": tp,
                "score": score,
                "strategies": strategies,
                "rsi": last["rsi"],
                "macd": last["macd_hist"],
                "cmf": last["cmf"],
                "atr_pct": last["atr_pct"],
            })

            time.sleep(0.05)
        except Exception:
            # Hata olsa bile diğer sembollere geç
            continue

    # Sıralama
    spot_sorted = sorted(
        spot_rows,
        key=lambda r: (-int(r["daily_long"]), -r["adx"])
    )
    score_sorted = sorted(
        score_rows,
        key=lambda r: -r["score"]
    )

    # HTML üretimi
    generate_spot_html(spot_sorted, "public/spot.html")
    generate_score_html(score_sorted, "public/score.html")

    print("✅ spot.html ve score.html üretildi")


if __name__ == "__main__":
    main()
