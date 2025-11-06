import os
import time
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD
from ta.momentum import RSIIndicator

BINANCE_SPOT_BASE = "https://api.binance.com"


def get_spot_symbols():
    """USDT quote, TRADING ve spot işlem serbest olan sembolleri döndürür."""
    url = f"{BINANCE_SPOT_BASE}/api/v3/exchangeInfo"
    data = requests.get(url, timeout=10).json()

    symbols = []
    for s in data["symbols"]:
        if (
            s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT"
            and s.get("isSpotTradingAllowed", False)
        ):
            symbols.append(s["symbol"])
    return symbols


def get_klines(symbol, interval="1d", limit=120):
    url = f"{BINANCE_SPOT_BASE}/api/v3/klines"
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


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if len(df) < 60:
        return df

    df = df.copy()
    df["ema9"] = EMAIndicator(df["close"], window=9).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], window=21).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], window=50).ema_indicator()
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    df["adx"] = ADXIndicator(
        high=df["high"], low=df["low"], close=df["close"], window=14
    ).adx()
    macd = MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd_line"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()
    df["vol_sma20"] = df["volume"].rolling(20).mean()
    return df


def daily_long_condition(last):
    needed = ["ema9", "ema21", "ema50", "rsi", "adx", "macd_line", "macd_signal", "vol_sma20"]
    if any(pd.isna(last[c]) for c in needed):
        return False

    cond = (
        last["ema9"] > last["ema21"] > last["ema50"]
        and 45 < last["rsi"] < 65
        and last["adx"] > 20
        and last["macd_line"] > last["macd_signal"]
        and last["volume"] > (last["vol_sma20"] or 0)
    )
    return cond


def weekly_long_condition(last):
    needed = ["ema9", "ema21", "rsi", "adx", "macd_line", "macd_signal"]
    if any(pd.isna(last[c]) for c in needed):
        return False

    cond = (
        last["ema9"] > last["ema21"]
        and last["rsi"] > 50
        and last["adx"] > 20
        and last["macd_line"] > last["macd_signal"]
    )
    return cond


def generate_html_report(rows, output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    html = []
    html.append("<!DOCTYPE html>")
    html.append("<html lang='en'>")
    html.append("<head>")
    html.append("<meta charset='UTF-8'>")
    html.append("<meta name='viewport' content='width=device-width, initial-scale=1.0'>")
    html.append("<title>Binance Spot Daily & Weekly Filter</title>")
    html.append(
        "<style>"
        "body{font-family:Arial, sans-serif;background:#020617;color:#e5e7eb;padding:20px;}"
        "a{color:#38bdf8;text-decoration:none;}"
        "a:hover{text-decoration:underline;}"
        "h1{color:#a855f7;text-align:center;}"
        "h2{color:#e5e7eb;text-align:center;font-size:14px;font-weight:normal;}"
        "table{width:100%;border-collapse:collapse;margin-top:20px;}"
        "th,td{padding:8px 10px;font-size:12px;text-align:right;}"
        "th{text-align:right;background:#111827;position:sticky;top:0;}"
        "tr:nth-child(even){background:#020617;}"
        "tr:nth-child(odd){background:#030712;}"
        ".sym{font-weight:bold;text-align:left;}"
        ".yes{color:#22c55e;font-weight:bold;}"
        ".no{color:#9ca3af;}"
        ".strong{background:#022c22;color:#bbf7d0;font-weight:bold;}"
        ".note{font-size:11px;color:#9ca3af;margin-top:10px;text-align:center;}"
        "</style>"
    )
    html.append("</head>")
    html.append("<body>")
    html.append("<h1>Binance Spot Daily & Weekly Filter (USDT)</h1>")
    html.append("<h2>Günlük (1D) ve Haftalık (1W) EMA / RSI / ADX / MACD filtreleri</h2>")
    html.append("<p class='note'>Bu rapor otomatik üretilmiştir, yatırım tavsiyesi değildir.</p>")
    html.append("<p class='note'><a href='index.html'>&larr; Ana sayfa</a></p>")

    html.append("<table>")
    html.append(
        "<tr>"
        "<th style='text-align:left;'>Symbol</th>"
        "<th>Last Price</th>"
        "<th>Daily Long</th>"
        "<th>Weekly Long</th>"
        "<th>RSI (D)</th>"
        "<th>RSI (W)</th>"
        "<th>ADX (D)</th>"
        "<th>ADX (W)</th>"
        "<th>MACD Hist (D)</th>"
        "<th>MACD Hist (W)</th>"
        "</tr>"
    )

    for r in rows:
        daily = r["daily_long"]
        weekly = r["weekly_long"]

        daily_cls = "yes" if daily else "no"
        weekly_cls = "yes" if weekly else "no"

        row_cls_start = "<tr>"
        if daily and weekly:
            row_cls_start = "<tr class='strong'>"

        def fmt(x, digits=6):
            return f"{x:.{digits}f}" if x is not None else "-"

        html.append(row_cls_start)
        html.append(f"<td class='sym'>{r['symbol']}</td>")
        html.append(f"<td>{fmt(r['last_price'], 6)}</td>")
        html.append(f"<td class='{daily_cls}'>{'YES' if daily else 'NO'}</td>")
        html.append(f"<td class='{weekly_cls}'>{'YES' if weekly else 'NO'}</td>")
        html.append(f"<td>{fmt(r['rsi_d'], 2)}</td>")
        html.append(f"<td>{fmt(r['rsi_w'], 2)}</td>")
        html.append(f"<td>{fmt(r['adx_d'], 2)}</td>")
        html.append(f"<td>{fmt(r['adx_w'], 2)}</td>")
        html.append(f"<td>{fmt(r['macd_hist_d'], 4)}</td>")
        html.append(f"<td>{fmt(r['macd_hist_w'], 4)}</td>")
        html.append("</tr>")

    html.append("</table>")
    html.append("</body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(html))


def main():
    print("▶ Spot semboller alınıyor...")
    symbols = get_spot_symbols()
    print(f"Toplam spot sembol: {len(symbols)}")

    rows = []

    for i, symbol in enumerate(symbols, start=1):
        try:
            print(f"[{i}/{len(symbols)}] {symbol} işleniyor...")
            df_d = get_klines(symbol, interval="1d", limit=120)
            if df_d is None or len(df_d) < 60:
                continue

            df_w = get_klines(symbol, interval="1w", limit=120)
            if df_w is None or len(df_w) < 60:
                continue

            df_d = compute_indicators(df_d)
            df_w = compute_indicators(df_w)

            last_d = df_d.iloc[-1]
            last_w = df_w.iloc[-1]

            daily_long = daily_long_condition(last_d)
            weekly_long = weekly_long_condition(last_w)

            if pd.isna(last_d["rsi"]) or pd.isna(last_w["rsi"]):
                continue

            last_price = last_d["close"]

            rows.append(
                {
                    "symbol": symbol,
                    "last_price": last_price,
                    "daily_long": daily_long,
                    "weekly_long": weekly_long,
                    "rsi_d": last_d["rsi"],
                    "rsi_w": last_w["rsi"],
                    "adx_d": last_d["adx"],
                    "adx_w": last_w["adx"],
                    "macd_hist_d": last_d["macd_hist"],
                    "macd_hist_w": last_w["macd_hist"],
                }
            )

            time.sleep(0.08)

        except Exception as e:
            print("Hata:", symbol, e)
            continue

    # Hem günlük hem haftalık long olanlar en üstte
    def sort_key(r):
        if r["daily_long"] and r["weekly_long"]:
            return (0, r["rsi_d"])
        elif r["daily_long"] or r["weekly_long"]:
            return (1, r["rsi_d"])
        else:
            return (2, r["rsi_d"])

    rows_sorted = sorted(rows, key=sort_key)

    output_path = os.path.join("public", "spot.html")
    generate_html_report(rows_sorted, output_path)
    print(f"✅ Spot raporu üretildi: {output_path}")


if __name__ == "__main__":
    main()
