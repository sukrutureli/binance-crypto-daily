import os
import time
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange  # 🔥 ATR eklendi

# Proxy üzerinden Binance SPOT
BINANCE_SPOT_BASE = "https://binance-proxy-63js.onrender.com/api"

# === 1️⃣ Güvenli Sembol Çekme ===
def get_spot_symbols(max_retries: int = 5, delay: float = 5.0):
    """
    USDT quote, TRADING ve spot işlem serbest olan tüm sembolleri döndürür.
    Rate limit veya ağ hatasında tekrar dener.
    """
    url = f"{BINANCE_SPOT_BASE}/api/v3/exchangeInfo"
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=15)
            data = resp.json()

            if not isinstance(data, dict) or "symbols" not in data:
                msg = str(data)
                if "Too many requests" in msg or "IP banned" in msg or "418" in msg:
                    print(f"🚫 Binance SPOT rate limit / ban (attempt {attempt}), bekleniyor...")
                    time.sleep(delay * attempt)
                    continue
                print(f"⚠️ [{attempt}] exchangeInfo beklenmedik yanıt: {msg}")
                time.sleep(delay)
                continue

            symbols = [
                s["symbol"]
                for s in data["symbols"]
                if s.get("status") == "TRADING"
                and s.get("quoteAsset") == "USDT"
                and s.get("isSpotTradingAllowed", False)
            ]
            if not symbols:
                print("⚠️ Spot sembol listesi boş döndü, tekrar denenecek...")
                time.sleep(delay)
                continue

            print(f"✅ {len(symbols)} spot sembol başarıyla alındı.")
            return symbols

        except requests.exceptions.RequestException as e:
            print(f"⚠️ [{attempt}] Bağlantı hatası: {e}, {delay}s sonra tekrar deneniyor...")
            time.sleep(delay)
        except Exception as e:
            print(f"❌ [{attempt}/{max_retries}] Beklenmeyen hata: {e}")
            time.sleep(delay)

    print("❌ Spot sembol listesi alınamadı, boş liste döndü.")
    return []


# === 2️⃣ Kline (Mum) Verisi ===
def get_klines(symbol, interval="1d", limit=120):
    url = f"{BINANCE_SPOT_BASE}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        if isinstance(data, dict) and data.get("code"):
            return None
        df = pd.DataFrame(
            data,
            columns=[
                "open_time","open","high","low","close","volume","close_time",
                "quote_volume","trades","taker_base_volume","taker_quote_volume","ignore"
            ],
        )
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df
    except Exception as e:
        print(f"⚠️ {symbol} klines alınamadı: {e}")
        return None


# === 3️⃣ Teknik Göstergeler ===
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
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

    # 🔥 ATR ekliyoruz (hem günlük hem haftalık için kullanılacak)
    atr = AverageTrueRange(
        high=df["high"],
        low=df["low"],
        close=df["close"],
        window=14
    )
    df["atr14"] = atr.average_true_range()

    return df


# === 4️⃣ Günlük ve Haftalık Filtreler ===
def daily_long_condition(last):
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","vol_sma20","atr14"]):
        return False
    return (
        last["ema9"] > last["ema21"] > last["ema50"]
        and 45 < last["rsi"] < 65
        and last["adx"] > 20
        and last["macd_line"] > last["macd_signal"]
        and last["volume"] > (last["vol_sma20"] or 0)
    )

def weekly_long_condition(last):
    if any(pd.isna(last.get(k)) for k in ["ema9","ema21","rsi","adx","macd_line","macd_signal","atr14"]):
        return False
    return (
        last["ema9"] > last["ema21"]
        and last["rsi"] > 50
        and last["adx"] > 20
        and last["macd_line"] > last["macd_signal"]
    )


# === 4.5️⃣ Entry / Stop / Target Hesaplama ===
def calc_entry_stop_target(last_row, kind: str):
    """
    kind = 'daily' veya 'weekly'
    Günlükte daha kısa, haftalıkta daha geniş hedef/stop.
    """
    price = last_row["close"]
    atr = last_row.get("atr14", None)

    if pd.isna(atr) or atr is None or atr <= 0:
        return None, None, None

    if kind == "daily":
        # Gün içi ufak kazanç
        entry = price
        stop = price - 1.5 * atr
        target = price + 2.5 * atr
    else:
        # Haftalık daha geniş hareket
        entry = price
        stop = price - 2.0 * atr
        target = price + 4.0 * atr

    return entry, stop, target


# === 5️⃣ HTML Rapor ===
def generate_html_report(rows, output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    html = []
    html.append("<!DOCTYPE html><html lang='en'><head>")
    html.append("<meta charset='UTF-8'><title>Binance Spot Daily & Weekly Filter</title>")
    html.append(
        "<style>"
        "body{font-family:Arial;background:#020617;color:#e5e7eb;padding:20px;}"
        "a{color:#38bdf8;text-decoration:none;}a:hover{text-decoration:underline;}"
        "h1{color:#a855f7;text-align:center;}"
        "p.note{text-align:center;font-size:11px;color:#9ca3af;}"
        "table{width:100%;border-collapse:collapse;margin-top:20px;}"
        "th,td{padding:8px 10px;font-size:11px;text-align:right;}"
        "th{background:#111827;position:sticky;top:0;z-index:1;}"
        "tr:nth-child(even){background:#020617;}tr:nth-child(odd){background:#030712;}"
        ".sym{text-align:left;font-weight:bold;}"
        ".yes{color:#22c55e;font-weight:bold;}"
        ".no{color:#9ca3af;}"
        ".strong{background:#022c22;color:#bbf7d0;}"
        ".buytag{background:#022c22;color:#bbf7d0;border-radius:999px;padding:2px 6px;font-size:10px;}"
        "</style>"
    )
    html.append("</head><body>")
    html.append("<h1>Binance Spot Daily & Weekly Filter (USDT)</h1>")
    html.append("<p class='note'>Günlük (intra-day) ve Haftalık (swing) için ayrı Entry / Stop / Target değerleri.</p>")
    html.append("<p class='note'><a href='index.html'>&larr; Ana sayfa</a></p>")

    html.append(
        "<table>"
        "<tr>"
        "<th style='text-align:left;'>Symbol</th>"
        "<th>Last</th>"

        "<th>Daily Long</th>"
        "<th>D Entry</th><th>D Stop</th><th>D Target</th>"

        "<th>Weekly Long</th>"
        "<th>W Entry</th><th>W Stop</th><th>W Target</th>"

        "<th>RSI (D)</th><th>RSI (W)</th>"
        "<th>ADX (D)</th><th>ADX (W)</th>"
        "<th>MACD Hist (D)</th><th>MACD Hist (W)</th>"
        "</tr>"
    )

    for r in rows:
        daily = r["daily_long"]
        weekly = r["weekly_long"]

        row_class = "strong" if daily and weekly else ""
        dcls = "yes" if daily else "no"
        wcls = "yes" if weekly else "no"

        def fmt(x, d=4):
            return f"{x:.{d}f}" if x is not None else "-"

        html.append(f"<tr class='{row_class}'>")
        html.append(f"<td class='sym'>{r['symbol']}</td>")
        html.append(f"<td>{fmt(r['last_price'])}</td>")

        # Daily
        html.append(f"<td class='{dcls}'>{'BUY' if daily else '-'}</td>")
        html.append(f"<td>{fmt(r['entry_d'])}</td>")
        html.append(f"<td>{fmt(r['stop_d'])}</td>")
        html.append(f"<td>{fmt(r['target_d'])}</td>")

        # Weekly
        html.append(f"<td class='{wcls}'>{'BUY' if weekly else '-'}</td>")
        html.append(f"<td>{fmt(r['entry_w'])}</td>")
        html.append(f"<td>{fmt(r['stop_w'])}</td>")
        html.append(f"<td>{fmt(r['target_w'])}</td>")

        # Indicators
        html.append(f"<td>{fmt(r['rsi_d'],2)}</td>")
        html.append(f"<td>{fmt(r['rsi_w'],2)}</td>")
        html.append(f"<td>{fmt(r['adx_d'],2)}</td>")
        html.append(f"<td>{fmt(r['adx_w'],2)}</td>")
        html.append(f"<td>{fmt(r['macd_hist_d'],4)}</td>")
        html.append(f"<td>{fmt(r['macd_hist_w'],4)}</td>")

        html.append("</tr>")

    html.append("</table></body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))


# === 6️⃣ Ana Akış ===
def main():
    print("▶ Spot semboller alınıyor...")
    symbols = get_spot_symbols()
    print(f"Toplam spot sembol: {len(symbols)}")

    rows = []
    for i, symbol in enumerate(symbols, start=1):
        try:
            print(f"[{i}/{len(symbols)}] {symbol} işleniyor...")
            df_d = get_klines(symbol, "1d", 120)
            df_w = get_klines(symbol, "1w", 120)
            if df_d is None or len(df_d) < 60 or df_w is None or len(df_w) < 60:
                continue

            df_d = compute_indicators(df_d)
            df_w = compute_indicators(df_w)

            last_d, last_w = df_d.iloc[-1], df_w.iloc[-1]

            daily_long = daily_long_condition(last_d)
            weekly_long = weekly_long_condition(last_w)

            # 🔥 Entry / Stop / Target hesapla
            entry_d = stop_d = target_d = None
            entry_w = stop_w = target_w = None

            if daily_long:
                entry_d, stop_d, target_d = calc_entry_stop_target(last_d, "daily")
            if weekly_long:
                entry_w, stop_w, target_w = calc_entry_stop_target(last_w, "weekly")

            rows.append({
                "symbol": symbol,
                "last_price": last_d["close"],
                "daily_long": daily_long,
                "weekly_long": weekly_long,

                "entry_d": entry_d,
                "stop_d": stop_d,
                "target_d": target_d,

                "entry_w": entry_w,
                "stop_w": stop_w,
                "target_w": target_w,

                "rsi_d": last_d["rsi"], "rsi_w": last_w["rsi"],
                "adx_d": last_d["adx"], "adx_w": last_w["adx"],
                "macd_hist_d": last_d["macd_hist"], "macd_hist_w": last_w["macd_hist"],
            })

            time.sleep(0.08)
        except Exception as e:
            print(f"⚠️ {symbol} hata: {e}")
            continue

    # Önce hem daily hem weekly BUY olanlar, sonra sadece biri, sonra diğerleri
    rows_sorted = sorted(rows, key=lambda r: (
        0 if (r["daily_long"] and r["weekly_long"]) else
        1 if (r["daily_long"] or r["weekly_long"]) else 2,
        -r["rsi_d"]
    ))

    out = "public/spot.html"
    generate_html_report(rows_sorted, out)
    print(f"✅ Spot raporu üretildi: {out}")


if __name__ == "__main__":
    main()
