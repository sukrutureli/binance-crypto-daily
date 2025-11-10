import os
import time
import requests
import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator, ROCIndicator
from ta.volatility import AverageTrueRange
from ta.volume import ChaikinMoneyFlowIndicator

BINANCE_SPOT_BASE = "https://binance-proxy-63js.onrender.com/api"

# === 1️⃣ Sembol Listesi ===
def get_spot_symbols(max_retries=5, delay=5.0):
    url = f"{BINANCE_SPOT_BASE}/api/v3/exchangeInfo"
    for attempt in range(1, max_retries + 1):
        try:
            data = requests.get(url, timeout=15).json()
            if "symbols" not in data:
                print(f"⚠️ Yanıt hatalı, {attempt}. deneme: {data}")
                time.sleep(delay)
                continue
            symbols = [
                s["symbol"]
                for s in data["symbols"]
                if s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("isSpotTradingAllowed", False)
            ]
            print(f"✅ {len(symbols)} spot sembol bulundu.")
            return symbols
        except Exception as e:
            print(f"⚠️ [{attempt}] Hata: {e}")
            time.sleep(delay)
    return []

# === 2️⃣ Kline Verisi ===
def get_klines(symbol, interval="1d", limit=120):
    url = f"{BINANCE_SPOT_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        if isinstance(data, dict): return None
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_volume","trades",
            "taker_base_volume","taker_quote_volume","ignore"
        ])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        print(f"⚠️ {symbol} kline hatası: {e}")
        return None

# === 3️⃣ Teknik Göstergeler ===
def compute_indicators(df):
    df = df.copy()
    df["ema9"] = EMAIndicator(df["close"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], 21).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()
    df["rsi"] = RSIIndicator(df["close"], 14).rsi()
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], 14).adx()
    macd = MACD(df["close"], 26, 12, 9)
    df["macd_line"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()
    df["vol_sma20"] = df["volume"].rolling(20).mean()
    df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()
    # Yeni göstergeler:
    df["cmf"] = ChaikinMoneyFlowIndicator(df["high"], df["low"], df["close"], df["volume"], 20).chaikin_money_flow()
    df["roc"] = ROCIndicator(df["close"], 9).roc()
    return df

# === 4️⃣ Günlük & Haftalık Filtreler (esnetilmiş) ===
def daily_long_condition(last):
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","rsi","adx","macd_line","macd_signal","vol_sma20"]):
        return False
    score = 0
    if last["ema9"] > last["ema21"]: score += 1
    if last["rsi"] > 38: score += 1
    if last["adx"] > 12: score += 1
    if last["macd_line"] > last["macd_signal"]: score += 1
    if last["volume"] > 0.9 * (last["vol_sma20"] or 1): score += 1
    return score >= 3  # Esnetilmiş: 3 veya daha fazla tutarsa BUY

def weekly_long_condition(last):
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","rsi","adx","macd_line","macd_signal"]):
        return False
    return (
        last["ema9"] > last["ema21"]
        and last["rsi"] > 48
        and last["adx"] > 18
        and last["macd_line"] > last["macd_signal"]
    )

# === 5️⃣ Giriş / Stop / Hedef ===
def calc_entry_stop_target(last, kind):
    price = last["close"]
    atr = last.get("atr14")
    if pd.isna(atr) or atr <= 0:
        return None, None, None
    if kind == "daily":
        return price, price - 1.2 * atr, price + 2.0 * atr
    else:
        return price, price - 1.7 * atr, price + 3.2 * atr

# === 6️⃣ Hacim & Momentum Analizi ===
def compute_strength(last):
    """0–100 arası sinyal gücü skoru"""
    score = 0
    vol_ratio = (last["volume"] / (last["vol_sma20"] or 1))
    cmf = last.get("cmf", 0)
    roc = last.get("roc", 0)

    # Trend
    if last["ema9"] > last["ema21"]: score += 20
    if last["rsi"] > 45: score += 10
    if last["adx"] > 20: score += 10
    if last["macd_line"] > last["macd_signal"]: score += 10

    # Hacim / Para Akışı
    if vol_ratio > 1.3: score += 15
    if cmf > 0.05: score += 15

    # Momentum
    if roc > 0: score += 10

    score = min(100, max(0, score))
    return score, vol_ratio, cmf, roc

# === 7️⃣ HTML Rapor ===
def generate_html_report(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    h = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>",
        "<title>Binance Spot Daily & Weekly Strength Report</title>",
        "<style>body{font-family:Arial;background:#020617;color:#e5e7eb;padding:20px;}table{width:100%;border-collapse:collapse;margin-top:15px;}"
        "th,td{padding:6px;font-size:11px;text-align:right;}th{background:#111827;}tr:nth-child(even){background:#0f172a;}tr:nth-child(odd){background:#1e293b;}"
        ".sym{text-align:left;font-weight:bold;}.yes{color:#22c55e;font-weight:bold;}.no{color:#9ca3af;}</style></head><body>",
        "<h1 style='text-align:center;color:#a855f7'>Binance Spot Günlük & Haftalık Analiz</h1>",
        "<p style='text-align:center;font-size:11px;color:#9ca3af'>Hacim, para akışı ve momentumla güçlendirilmiş sinyal tablosu</p>",
        "<table><tr><th style='text-align:left'>Symbol</th><th>Last</th>"
        "<th>Daily</th><th>D Entry</th><th>D Stop</th><th>D Target</th>"
        "<th>Weekly</th><th>W Entry</th><th>W Stop</th><th>W Target</th>"
        "<th>Vol xAvg</th><th>CMF</th><th>ROC%</th><th>Score</th></tr>"
    ]
    for r in rows:
        fmt = lambda x, d=4: f"{x:.{d}f}" if x is not None else "-"
        dcls = "yes" if r["daily_long"] else "no"
        wcls = "yes" if r["weekly_long"] else "no"
        h.append(
            f"<tr><td class='sym'>{r['symbol']}</td><td>{fmt(r['last_price'])}</td>"
            f"<td class='{dcls}'>{'BUY' if r['daily_long'] else '-'}</td>"
            f"<td>{fmt(r['entry_d'])}</td><td>{fmt(r['stop_d'])}</td><td>{fmt(r['target_d'])}</td>"
            f"<td class='{wcls}'>{'BUY' if r['weekly_long'] else '-'}</td>"
            f"<td>{fmt(r['entry_w'])}</td><td>{fmt(r['stop_w'])}</td><td>{fmt(r['target_w'])}</td>"
            f"<td>{fmt(r['vol_ratio'],2)}</td><td>{fmt(r['cmf'],3)}</td>"
            f"<td>{fmt(r['roc'],2)}</td><td>{r['strength']}</td></tr>"
        )
    h.append("</table></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))

# === 8️⃣ Ana Akış ===
def main():
    print("▶ Spot semboller alınıyor...")
    symbols = get_spot_symbols()
    print(f"Toplam spot sembol: {len(symbols)}")
    rows = []
    for i, sym in enumerate(symbols, start=1):
        try:
            print(f"[{i}/{len(symbols)}] {sym} işleniyor...")
            df_d = get_klines(sym, "1d", 120)
            df_w = get_klines(sym, "1w", 120)
            if df_d is None or len(df_d) < 60 or df_w is None or len(df_w) < 60:
                continue

            df_d, df_w = compute_indicators(df_d), compute_indicators(df_w)
            last_d, last_w = df_d.iloc[-1], df_w.iloc[-1]

            daily_long = daily_long_condition(last_d)
            weekly_long = weekly_long_condition(last_w)

            entry_d, stop_d, target_d = (calc_entry_stop_target(last_d, "daily") if daily_long else (None, None, None))
            entry_w, stop_w, target_w = (calc_entry_stop_target(last_w, "weekly") if weekly_long else (None, None, None))

            strength, vol_ratio, cmf, roc = compute_strength(last_d)

            rows.append({
                "symbol": sym,
                "last_price": last_d["close"],
                "daily_long": daily_long, "weekly_long": weekly_long,
                "entry_d": entry_d, "stop_d": stop_d, "target_d": target_d,
                "entry_w": entry_w, "stop_w": stop_w, "target_w": target_w,
                "vol_ratio": vol_ratio, "cmf": cmf, "roc": roc,
                "strength": strength
            })
            time.sleep(0.07)
        except Exception as e:
            print(f"⚠️ {sym} hata: {e}")
            continue

    rows_sorted = sorted(rows, key=lambda r: (-r["strength"], -r["vol_ratio"]))
    out = "public/spot.html"
    generate_html_report(rows_sorted, out)
    print(f"✅ Spot raporu üretildi: {out}")

if __name__ == "__main__":
    main()
