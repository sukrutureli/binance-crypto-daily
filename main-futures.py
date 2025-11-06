import os
import time
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

BINANCE_FAPI_BASE = "https://fapi.binance.com"


def get_futures_symbols():
    """USDT margined, TRADING durumda olan tüm futures sembollerini döndürür."""
    url = f"{BINANCE_FAPI_BASE}/fapi/v1/exchangeInfo"
    data = requests.get(url, timeout=10).json()

    symbols = []
    for s in data["symbols"]:
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            symbols.append(s["symbol"])
    return symbols


def get_klines(symbol, interval="1d", limit=120):
    url = f"{BINANCE_FAPI_BASE}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = requests.get(url, params=params, timeout=10).json()
    if isinstance(data, dict) and data.get("code"):
        return None

    df = pd.DataFrame(
        data,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_base_volume",
            "taker_quote_volume",
            "ignore",
        ],
    )
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df


def get_funding_rate(symbol):
    """Son funding rate değerini döndürür."""
    url = f"{BINANCE_FAPI_BASE}/fapi/v1/fundingRate"
    params = {"symbol": symbol, "limit": 1}
    data = requests.get(url, params=params, timeout=10).json()
    if not data or isinstance(data, dict) and data.get("code"):
        return None
    return float(data[0]["fundingRate"])


def get_open_interest_change(symbol):
    """
    Son 2 gün için open interest alır,
    yüzde değişimi hesaplar.
    """
    url = f"{BINANCE_FAPI_BASE}/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": "1d", "limit": 2}
    data = requests.get(url, params=params, timeout=10).json()
    if not isinstance(data, list) or len(data) < 2:
        return None

    prev_oi = float(data[0]["sumOpenInterest"])
    last_oi = float(data[1]["sumOpenInterest"])
    if prev_oi == 0:
        return None
    change_pct = (last_oi - prev_oi) / prev_oi * 100.0
    return change_pct


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """EMA, RSI, ADX, MACD, ATR gibi indikatörleri DF üzerine ekler."""
    if len(df) < 60:
        return df

    df = df.copy()

    # EMA'lar
    df["ema9"] = EMAIndicator(df["close"], window=9).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], window=21).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], window=50).ema_indicator()

    # RSI
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()

    # ADX
    df["adx"] = ADXIndicator(
        high=df["high"], low=df["low"], close=df["close"], window=14
    ).adx()

    # MACD
    macd = MACD(
        close=df["close"], window_slow=26, window_fast=12, window_sign=9
    )
    df["macd_line"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # Hacim ortalaması
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    # ATR
    atr_ind = AverageTrueRange(
        high=df["high"], low=df["low"], close=df["close"], window=14
    )
    df["atr14"] = atr_ind.average_true_range()

    return df


def evaluate_signal(last_row, funding_rate: float | None, oi_change: float | None):
    """
    Long/short filtre mantığını uygular.
    Dönen değer: "LONG" / "SHORT" / "-"
    """
    needed = ["ema9", "ema21", "ema50", "rsi", "adx", "macd_line", "macd_signal", "vol_sma20", "atr14"]
    if any(pd.isna(last_row[c]) for c in needed):
        return "-"

    if funding_rate is None or oi_change is None:
        return "-"

    # LONG koşulu
    long_cond = (
        last_row["ema9"] > last_row["ema21"] > last_row["ema50"]
        and 45 < last_row["rsi"] < 65
        and last_row["adx"] > 25
        and last_row["macd_line"] > last_row["macd_signal"]
        and 0 < funding_rate < 0.02
        and oi_change > 0
        and last_row["volume"] > (last_row["vol_sma20"] or 0)
    )

    # SHORT koşulu
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


def calc_entry_stop_target(signal: str, last_price: float, atr14: float):
    """ATR'ye göre entry/stop/target hesaplar."""
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


def generate_html_report(rows, output_path: str):
    """Futures raporunu futures.html olarak üretir."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    html = []
    html.append("<!DOCTYPE html>")
    html.append("<html lang='en'>")
    html.append("<head>")
    html.append("<meta charset='UTF-8'>")
    html.append("<meta name='viewport' content='width=device-width, initial-scale=1.0'>")
    html.append("<title>Binance Futures Daily Filter</title>")
    html.append(
        "<style>"
        "body{font-family:Arial, sans-serif;background:#0b1120;color:#e5e7eb;padding:20px;}"
        "a{color:#38bdf8;text-decoration:none;}"
        "a:hover{text-decoration:underline;}"
        "h1{color:#facc15;text-align:center;}"
        "h2{color:#e5e7eb;text-align:center;font-size:14px;font-weight:normal;}"
        "table{width:100%;border-collapse:collapse;margin-top:20px;}"
        "th,td{padding:8px 10px;font-size:12px;text-align:right;}"
        "th{text-align:right;background:#111827;position:sticky;top:0;}"
        "tr:nth-child(even){background:#020617;}"
        "tr:nth-child(odd){background:#030712;}"
        ".sym{font-weight:bold;text-align:left;}"
        ".long{color:#22c55e;font-weight:bold;}"
        ".short{color:#f97316;font-weight:bold;}"
        ".neutral{color:#9ca3af;}"
        ".tag{font-size:11px;border-radius:999px;padding:2px 6px;}"
        ".tag-long{background:#022c22;color:#bbf7d0;}"
        ".tag-short{background:#451a03;color:#fed7aa;}"
        ".tag-neutral{background:#020617;color:#9ca3af;}"
        ".note{font-size:11px;color:#9ca3af;margin-top:10px;text-align:center;}"
        "</style>"
    )
    html.append("</head>")
    html.append("<body>")
    html.append("<h1>Binance Futures Daily Filter (USDT-M)</h1>")
    html.append("<h2>Günlük (1D) EMA / RSI / ADX / MACD / Funding / OI filtresi + ATR ile Entry / Stop / Target hesabı</h2>")
    html.append("<p class='note'>Bu rapor otomatik üretilmiştir, yatırım tavsiyesi değildir. Kendi risk yönetimini mutlaka uygula.</p>")
    html.append("<p class='note'><a href='index.html'>&larr; Ana sayfa</a></p>")

    html.append("<table>")
    html.append(
        "<tr>"
        "<th style='text-align:left;'>Symbol</th>"
        "<th>Signal</th>"
        "<th>Entry</th>"
        "<th>Stop</th>"
        "<th>Target</th>"
        "<th>Last</th>"
        "<th>Funding</th>"
        "<th>OI Δ %</th>"
        "<th>RSI</th>"
        "<th>ADX</th>"
        "<th>MACD Hist</th>"
        "</tr>"
    )

    for r in rows:
        signal = r["signal"]
        if signal == "LONG":
            sig_class = "long"
            tag_class = "tag tag-long"
            sig_text = "✅ LONG"
        elif signal == "SHORT":
            sig_class = "short"
            tag_class = "tag tag-short"
            sig_text = "❌ SHORT"
        else:
            sig_class = "neutral"
            tag_class = "tag tag-neutral"
            sig_text = "• NEUTRAL"

        def fmt(x, digits=6):
            return f"{x:.{digits}f}" if x is not None else "-"

        html.append("<tr>")
        html.append(f"<td class='sym'>{r['symbol']}</td>")
        html.append(f"<td class='{sig_class}'><span class='{tag_class}'>{sig_text}</span></td>")
        html.append(f"<td>{fmt(r['entry'])}</td>")
        html.append(f"<td>{fmt(r['stop'])}</td>")
        html.append(f"<td>{fmt(r['target'])}</td>")
        html.append(f"<td>{fmt(r['last_price'])}</td>")
        html.append(f"<td>{r['funding_rate']:.5f}</td>")
        html.append(f"<td>{r['oi_change']:.2f}</td>")
        html.append(f"<td>{r['rsi']:.2f}</td>")
        html.append(f"<td>{r['adx']:.2f}</td>")
        html.append(f"<td>{r['macd_hist']:.4f}</td>")
        html.append("</tr>")

    html.append("</table>")
    html.append("</body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))


def main():
    print("▶ Futures semboller alınıyor...")
    symbols = get_futures_symbols()
    print(f"Toplam futures sembol: {len(symbols)}")

    rows = []

    for i, symbol in enumerate(symbols, start=1):
        try:
            print(f"[{i}/{len(symbols)}] {symbol} işleniyor...")
            df = get_klines(symbol)
            if df is None or len(df) < 60:
                continue

            df = compute_indicators(df)
            last = df.iloc[-1]

            # Son close fiyat
            last_price = last["close"]

            # Funding
            funding_rate = get_funding_rate(symbol)

            # OI değişimi
            oi_change = get_open_interest_change(symbol)

            signal = evaluate_signal(last, funding_rate, oi_change)

            if pd.isna(last["rsi"]) or pd.isna(last["adx"]) or pd.isna(last["macd_hist"]):
                continue

            entry, stop, target = calc_entry_stop_target(signal, last_price, last["atr14"])

            rows.append(
                {
                    "symbol": symbol,
                    "signal": signal,
                    "entry": entry,
                    "stop": stop,
                    "target": target,
                    "last_price": last_price,
                    "funding_rate": funding_rate if funding_rate is not None else 0.0,
                    "oi_change": oi_change if oi_change is not None else 0.0,
                    "rsi": last["rsi"],
                    "adx": last["adx"],
                    "macd_hist": last["macd_hist"],
                }
            )

            time.sleep(0.08)

        except Exception as e:
            print("Hata:", symbol, e)
            continue

    # LONG / SHORT olanlar üstte
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            0 if r["signal"] == "LONG"
            else 1 if r["signal"] == "SHORT"
            else 2,
            -abs(r["oi_change"])
        ),
    )

    output_path = os.path.join("public", "futures.html")
    generate_html_report(rows_sorted, output_path)
    print(f"✅ Futures raporu üretildi: {output_path}")


if __name__ == "__main__":
    main()
