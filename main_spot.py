import os
import time
import requests
import pandas as pd
from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volume import ChaikinMoneyFlowIndicator

BINANCE_SPOT_BASE = "https://binance-proxy-63js.onrender.com/api"

# === 1️⃣ Güvenli Sembol Çekme ===
def get_spot_symbols(max_retries: int = 5, delay: float = 5.0):
    url = f"{BINANCE_SPOT_BASE}/api/v3/exchangeInfo"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=15)
            data = resp.json()
            if not isinstance(data, dict) or "symbols" not in data:
                time.sleep(delay)
                continue
            symbols = [
                s["symbol"] for s in data["symbols"]
                if s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("isSpotTradingAllowed", False)
            ]
            if symbols:
                print(f"✅ {len(symbols)} spot sembol bulundu.")
                return symbols
            time.sleep(delay)
        except Exception as e:
            print(f"⚠️ [{attempt}] bağlantı hatası: {e}")
            time.sleep(delay)
    print("❌ Sembol listesi alınamadı.")
    return []

# === 2️⃣ Kline (Mum) Verisi ===
def get_klines(symbol, interval="1d", limit=120):
    url = f"{BINANCE_SPOT_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        if isinstance(data, dict):
            return None
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume","close_time",
            "quote_volume","trades","taker_base_volume","taker_quote_volume","ignore"
        ])
        df[["open","high","low","close","volume"]] = df[["open","high","low","close","volume"]].astype(float)
        return df
    except Exception as e:
        print(f"⚠️ {symbol} klines alınamadı: {e}")
        return None

# === 3️⃣ Teknik Göstergeler ===
def compute_indicators(df):
    if len(df) < 60:
        return df
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
    df["vol_ratio"] = df["volume"] / df["vol_sma20"]
    df["cmf"] = ChaikinMoneyFlowIndicator(df["high"], df["low"], df["close"], df["volume"], 20).chaikin_money_flow()
    return df

# === 4️⃣ Gelişmiş Günlük ve Haftalık Koşullar ===
def daily_condition(df):
    last = df.iloc[-1]
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","cmf","vol_ratio"]):
        return False
    if last["ema9"] > last["ema21"] > last["ema50"] \
        and last["rsi"] > 45 and last["adx"] > 20 \
        and last["macd_line"] > last["macd_signal"] \
        and last["vol_ratio"] > 1.2 and last["cmf"] > 0 \
        and last["rsi"] > df["rsi"].iloc[-3] \
        and abs((last["close"] - last["ema9"]) / last["ema9"]) < 0.03:
        return True
    return False

def weekly_condition(df):
    last = df.iloc[-1]
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","rsi","adx","macd_line","macd_signal","cmf"]):
        return False
    if last["ema9"] > last["ema21"] and last["rsi"] > 50 \
        and last["adx"] > 20 and last["macd_line"] > last["macd_signal"] \
        and last["cmf"] > 0:
        return True
    return False

# === 5️⃣ Giriş / Stop / Hedef Hesapla ===
def calc_entry_stop_target(df):
    last = df.iloc[-1]
    atr = (last["high"] - last["low"]) * 0.5
    entry = last["close"]
    stop = entry - 1.5 * atr
    target = entry + 2.0 * atr
    return entry, stop, target

# === 6️⃣ HTML Rapor ===
def generate_html(rows, out):
    os.makedirs(os.path.dirname(out), exist_ok=True)
    html = [
        "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Binance Spot Günlük & Haftalık</title>",
        "<style>body{font-family:Arial;background:#020617;color:#e5e7eb;padding:20px;}h1{text-align:center;color:#a855f7;}table{width:100%;border-collapse:collapse;}th,td{padding:8px;text-align:right;}th{background:#111827;}tr:nth-child(even){background:#030712;}tr:nth-child(odd){background:#020617;}.sym{text-align:left;font-weight:bold;}.buy{color:#22c55e;font-weight:bold;}</style>",
        "</head><body><h1>Binance Spot Günlük & Haftalık (USDT)</h1><p><a href='index.html' style='color:#38bdf8;'>&larr; Ana sayfa</a></p>",
        "<table><tr><th style='text-align:left;'>Symbol</th><th>Type</th><th>Entry</th><th>Stop</th><th>Target</th><th>RSI</th><th>ADX</th><th>Vol Ratio</th><th>CMF</th></tr>"
    ]
    for r in rows:
        def fmt(x,d=4): return f"{x:.{d}f}" if x else "-"
        html.append(f"<tr><td class='sym'>{r['symbol']}</td><td class='buy'>{r['type']}</td><td>{fmt(r['entry'])}</td><td>{fmt(r['stop'])}</td><td>{fmt(r['target'])}</td><td>{fmt(r['rsi'],2)}</td><td>{fmt(r['adx'],2)}</td><td>{fmt(r['vol_ratio'],2)}</td><td>{fmt(r['cmf'],2)}</td></tr>")
    html.append("</table></body></html>")
    with open(out, "w", encoding="utf-8") as f: f.write("\n".join(html))

# === 7️⃣ Ana Akış ===
def main():
    print("▶ Spot semboller alınıyor...")
    symbols = get_spot_symbols()
    rows = []
    for i, s in enumerate(symbols, 1):
        try:
            print(f"[{i}/{len(symbols)}] {s}")
            d = get_klines(s, "1d", 120)
            w = get_klines(s, "1w", 120)
            if d is None or len(d)<60 or w is None or len(w)<60: continue
            d, w = compute_indicators(d), compute_indicators(w)
            if daily_condition(d):
                entry, stop, target = calc_entry_stop_target(d)
                rows.append({
                    "symbol": s, "type": "DAILY BUY",
                    "entry": entry, "stop": stop, "target": target,
                    "rsi": d.iloc[-1]["rsi"], "adx": d.iloc[-1]["adx"],
                    "vol_ratio": d.iloc[-1]["vol_ratio"], "cmf": d.iloc[-1]["cmf"]
                })
            if weekly_condition(w):
                entry, stop, target = calc_entry_stop_target(w)
                rows.append({
                    "symbol": s, "type": "WEEKLY BUY",
                    "entry": entry, "stop": stop, "target": target,
                    "rsi": w.iloc[-1]["rsi"], "adx": w.iloc[-1]["adx"],
                    "vol_ratio": w.iloc[-1].get("vol_ratio", 1), "cmf": w.iloc[-1]["cmf"]
                })
            time.sleep(0.08)
        except Exception as e:
            print(f"⚠️ {s} hata: {e}")
            continue
    rows_sorted = sorted(rows, key=lambda r: (-r["vol_ratio"], -r["cmf"], -r["rsi"]))
    generate_html(rows_sorted, "public/spot.html")
    print(f"✅ Rapor üretildi: public/spot.html")

if __name__ == "__main__":
    main()
