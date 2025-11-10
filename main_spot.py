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
    df["cmf"] = ChaikinMoneyFlowIndicator(df["high"], df["low"], df["close"], df["volume"], 20).chaikin_money_flow()
    df["roc"] = ROCIndicator(df["close"], 9).roc()
    return df

# === 4️⃣ Filtreler (Soft Mode) ===
def daily_long_condition(last):
    """Günlük sinyali biraz gevşetilmiş filtre."""
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","vol_sma20"]):
        return False
    return (
        # Trend yapısı: 9 > 21 > 50 şartı kaldırıldı, yerine 9 > 21 ve yakınlık kontrolü
        last["ema9"] > last["ema21"] * 0.995
        and last["rsi"] > 42 and last["rsi"] < 68
        and last["adx"] > 15
        and last["macd_line"] > last["macd_signal"]
        and last["volume"] > 0.9 * (last["vol_sma20"] or 1)
        and last.get("cmf", 0) > -0.05  # para akışı çok negatif olmasın
    )

def weekly_long_condition(last):
    """Haftalık trend onayı (değişmedi)."""
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
        return price, price - 1.2 * atr, price + 1.8 * atr
    else:
        return price, price - 1.8 * atr, price + 3.0 * atr

# === 6️⃣ HTML ===
def generate_html_report(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    h = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>",
        "<title>Binance Spot Günlük & Haftalık Analiz (Soft Mode)</title>",
        "<style>body{font-family:Arial;background:#020617;color:#e5e7eb;padding:20px;}table{width:100%;border-collapse:collapse;margin-top:15px;}"
        "th,td{padding:6px;font-size:11px;text-align:right;}th{background:#111827;}tr:nth-child(even){background:#0f172a;}tr:nth-child(odd){background:#1e293b;}"
        ".sym{text-align:left;font-weight:bold;}.yes{color:#22c55e;font-weight:bold;}.no{color:#9ca3af;}</style></head><body>",
        "<h1 style='text-align:center;color:#a855f7'>Binance Spot Günlük & Haftalık Analiz</h1>",
        "<p style='text-align:center;font-size:11px;color:#9ca3af'>Soft mode: potansiyel trend başlangıçlarını da dahil eder</p>",
        "<table><tr><th style='text-align:left'>Symbol</th><th>Last</th>"
        "<th>Daily</th><th>D Entry</th><th>D Stop</th><th>D Target</th>"
        "<th>Weekly</th><th>W Entry</th><th>W Stop</th><th>W Target</th>"
        "<th>RSI</th><th>ADX</th><th>Vol xAvg</th><th>CMF</th></tr>"
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
            f"<td>{fmt(r['rsi'],2)}</td><td>{fmt(r['adx'],2)}</td><td>{fmt(r['vol_ratio'],2)}</td><td>{fmt(r['cmf'],3)}</td></tr>"
        )
    h.append("</table></body></html>")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))

# === 7️⃣ Ana Akış ===
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

            rows.append({
                "symbol": sym,
                "last_price": last_d["close"],
                "daily_long": daily_long, "weekly_long": weekly_long,
                "entry_d": entry_d, "stop_d": stop_d, "target_d": target_d,
                "entry_w": entry_w, "stop_w": stop_w, "target_w": target_w,
                "rsi": last_d["rsi"], "adx": last_d["adx"],
                "vol_ratio": last_d["volume"] / (last_d["vol_sma20"] or 1),
                "cmf": last_d.get("cmf", 0),
            })
            time.sleep(0.07)
        except Exception as e:
            print(f"⚠️ {sym} hata: {e}")
            continue

    rows_sorted = sorted(rows, key=lambda r: (not r["daily_long"], not r["weekly_long"], -r["adx"]))
    out = "public/spot.html"
    generate_html_report(rows_sorted, out)
    print(f"✅ Spot raporu üretildi: {out}")

if __name__ == "__main__":
    main()
