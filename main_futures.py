import os
import time
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

BINANCE_FAPI_BASE = "https://binance-proxy-63js.onrender.com/fapi"

# === 1️⃣ Binance Sembolleri Güvenli Çekme ===
def get_futures_symbols(max_retries: int = 5, delay: float = 5.0):
    """
    USDT margined, TRADING durumda olan tüm Binance Futures sembollerini döndürür.
    Rate limit veya bağlantı hatasında birkaç kez tekrar dener.
    """
    url = f"{BINANCE_FAPI_BASE}/fapi/v1/exchangeInfo"

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=15)
            data = resp.json()

            # ⚠️ Binance rate limit veya IP ban durumları
            if not isinstance(data, dict):
                print(f"⚠️ [{attempt}] Beklenmedik yanıt formatı, tekrar deneniyor...")
                time.sleep(delay)
                continue

            if "symbols" not in data:
                msg = str(data)
                if "Too many requests" in msg or "IP banned" in msg or "418" in msg:
                    print(f"🚫 Binance rate limit / ban yanıtı (attempt {attempt}), bekleniyor...")
                    time.sleep(delay * attempt)
                    continue
                else:
                    print(f"⚠️ [{attempt}] API yanıtında 'symbols' yok: {msg}")
                    time.sleep(delay)
                    continue

            symbols = []
            for s in data["symbols"]:
                if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
                    symbols.append(s["symbol"])

            if not symbols:
                print("⚠️ Binance sembol listesi boş döndü, tekrar deneniyor...")
                time.sleep(delay)
                continue

            print(f"✅ {len(symbols)} futures sembol başarıyla alındı.")
            return symbols

        except requests.exceptions.RequestException as e:
            print(f"⚠️ [{attempt}] Bağlantı hatası: {e}, {delay}s sonra tekrar denenecek...")
            time.sleep(delay)
        except Exception as e:
            print(f"❌ Beklenmeyen hata ({attempt}/{max_retries}): {e}")
            time.sleep(delay)

    print("❌ Binance sembol listesi alınamadı, script devam ediyor (boş liste döndü).")
    return []


# === 2️⃣ Kline (Mum) Verisi ===
def get_klines(symbol, interval="1d", limit=120):
    url = f"{BINANCE_FAPI_BASE}/fapi/v1/klines"
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
        print(f"⚠️ {symbol} klines çekilemedi: {e}")
        return None


# === 3️⃣ Funding ve OI Fonksiyonları ===
def get_funding_rate(symbol):
    url = f"{BINANCE_FAPI_BASE}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        if not data or isinstance(data, dict) and data.get("code"):
            return None
        return float(data[0]["fundingRate"])
    except Exception:
        return None


def get_open_interest_change(symbol):
    url = f"{BINANCE_FAPI_BASE}/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": "1d", "limit": 2}
    try:
        data = requests.get(url, params=params, timeout=10).json()
        if not isinstance(data, list) or len(data) < 2:
            return None
        prev_oi = float(data[0]["sumOpenInterest"])
        last_oi = float(data[1]["sumOpenInterest"])
        if prev_oi == 0:
            return None
        return (last_oi - prev_oi) / prev_oi * 100.0
    except Exception:
        return None


# === 4️⃣ Teknik Göstergeleri Hesapla ===
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
    df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()
    return df


# === 5️⃣ Filtre Mantığı ===
def evaluate_signal(last_row, funding_rate: float | None, oi_change: float | None):
    needed = ["ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","vol_sma20","atr14"]
    if any(pd.isna(last_row.get(c)) for c in needed):
        return "-"
    if funding_rate is None or oi_change is None:
        return "-"
    long_cond = (
        last_row["ema9"] > last_row["ema21"] > last_row["ema50"]
        and 45 < last_row["rsi"] < 65
        and last_row["adx"] > 25
        and last_row["macd_line"] > last_row["macd_signal"]
        and 0 < funding_rate < 0.02
        and oi_change > 0
        and last_row["volume"] > (last_row["vol_sma20"] or 0)
    )
    short_cond = (
        last_row["ema9"] < last_row["ema21"] < last_row["ema50"]
        and 35 < last_row["rsi"] < 55
        and last_row["adx"] > 25
        and last_row["macd_line"] < last_row["macd_signal"]
        and -0.02 < funding_rate < 0
        and oi_change > 0
        and last_row["volume"] > (last_row["vol_sma20"] or 0)
    )
    if long_cond:
        return "LONG"
    if short_cond:
        return "SHORT"
    return "-"


# === 6️⃣ Giriş / Stop / Target ===
def calc_entry_stop_target(signal: str, last_price: float, atr14: float):
    if pd.isna(atr14) or atr14 <= 0:
        return last_price, None, None
    if signal == "LONG":
        entry = last_price
        stop = last_price - 1.5 * atr14
        target = last_price + 2.0 * atr14
        return entry, stop, target
    elif signal == "SHORT":
        entry = last_price
        stop = last_price + 1.5 * atr14
        target = last_price - 2.0 * atr14
        return entry, stop, target
    else:
        return last_price, None, None


# === 7️⃣ HTML Rapor ===
def generate_html_report(rows, output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    html = []
    html.append("<!DOCTYPE html><html lang='en'><head>")
    html.append("<meta charset='UTF-8'><title>Binance Futures Daily Filter</title>")
    html.append(
        "<style>body{font-family:Arial;background:#0b1120;color:#e5e7eb;padding:20px;}"
        "a{color:#38bdf8;text-decoration:none;}a:hover{text-decoration:underline;}"
        "h1{color:#facc15;text-align:center;}table{width:100%;border-collapse:collapse;margin-top:20px;}"
        "th,td{padding:8px 10px;font-size:12px;text-align:right;}"
        "th{background:#111827;position:sticky;top:0;}tr:nth-child(even){background:#020617;}tr:nth-child(odd){background:#030712;}"
        ".sym{text-align:left;font-weight:bold;}.long{color:#22c55e;font-weight:bold;}.short{color:#f97316;font-weight:bold;}.neutral{color:#9ca3af;}"
        ".tag{font-size:11px;border-radius:999px;padding:2px 6px;}.tag-long{background:#022c22;color:#bbf7d0;}.tag-short{background:#451a03;color:#fed7aa;}.tag-neutral{background:#020617;color:#9ca3af;}"
        "</style>"
    )
    html.append("</head><body>")
    html.append("<h1>Binance Futures Daily Filter (USDT-M)</h1>")
    html.append("<p><a href='index.html'>&larr; Ana sayfa</a></p>")
    html.append("<table><tr><th style='text-align:left;'>Symbol</th><th>Signal</th><th>Entry</th><th>Stop</th><th>Target</th><th>Last</th><th>Funding</th><th>OI Δ%</th><th>RSI</th><th>ADX</th><th>MACD</th></tr>")
    for r in rows:
        sig = r["signal"]
        cls = "neutral"; tag="• NEUTRAL"; style="tag tag-neutral"
        if sig=="LONG": cls="long"; tag="✅ LONG"; style="tag tag-long"
        elif sig=="SHORT": cls="short"; tag="❌ SHORT"; style="tag tag-short"
        def fmt(x,d=4): return f"{x:.{d}f}" if x is not None else "-"
        html.append(f"<tr><td class='sym'>{r['symbol']}</td><td class='{cls}'><span class='{style}'>{tag}</span></td>"
                    f"<td>{fmt(r['entry'])}</td><td>{fmt(r['stop'])}</td><td>{fmt(r['target'])}</td>"
                    f"<td>{fmt(r['last_price'])}</td><td>{fmt(r['funding_rate'],5)}</td><td>{fmt(r['oi_change'],2)}</td>"
                    f"<td>{fmt(r['rsi'],2)}</td><td>{fmt(r['adx'],2)}</td><td>{fmt(r['macd_hist'],4)}</td></tr>")
    html.append("</table></body></html>")
    with open(output_path,"w",encoding="utf-8") as f:f.write("\n".join(html))


# === 8️⃣ Ana Akış ===
def main():
    print("▶ Futures semboller alınıyor...")
    symbols = get_futures_symbols()
    print(f"Toplam futures sembol: {len(symbols)}")
    rows=[]
    for i,symbol in enumerate(symbols,1):
        try:
            print(f"[{i}/{len(symbols)}] {symbol} işleniyor...")
            df=get_klines(symbol)
            if df is None or len(df)<60: continue
            df=compute_indicators(df)
            last=df.iloc[-1]
            last_price=last["close"]
            funding_rate=get_funding_rate(symbol)
            oi_change=get_open_interest_change(symbol)
            signal=evaluate_signal(last,funding_rate,oi_change)
            entry,stop,target=calc_entry_stop_target(signal,last_price,last["atr14"])
            rows.append({"symbol":symbol,"signal":signal,"entry":entry,"stop":stop,"target":target,
                         "last_price":last_price,"funding_rate":funding_rate or 0.0,
                         "oi_change":oi_change or 0.0,"rsi":last["rsi"],"adx":last["adx"],"macd_hist":last["macd_hist"]})
            time.sleep(0.08)
        except Exception as e:
            print(f"⚠️ {symbol} hata: {e}")
            continue
    rows_sorted=sorted(rows,key=lambda r:(0 if r["signal"]=="LONG" else 1 if r["signal"]=="SHORT" else 2,-abs(r["oi_change"])))
    out="public/futures.html";generate_html_report(rows_sorted,out)
    print(f"✅ Futures raporu üretildi: {out}")

if __name__=="__main__":
    main()
