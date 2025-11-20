import os
import time
import requests
import pandas as pd

from ta.trend import EMAIndicator, ADXIndicator, MACD, SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange, BollingerBands
from ta.volume import ChaikinMoneyFlowIndicator, OnBalanceVolumeIndicator


BINANCE_SPOT_BASE = "https://binance-proxy-63js.onrender.com/api"

STABLECOINS = {"USDT", "BUSD", "DAI", "USDC", "TUSD", "FDUSD"}


# ============================================================
# 1) Spot Sembol Listesi (stablecoin filtreli)
# ============================================================
def get_spot_symbols():
    url = f"{BINANCE_SPOT_BASE}/api/v3/exchangeInfo"
    data = requests.get(url, timeout=15).json()

    symbols = []
    for s in data.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        if s.get("isSpotTradingAllowed") is False:
            continue
        if s["baseAsset"] in STABLECOINS:
            continue
        symbols.append(s["symbol"])

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

    df["ema9"] = EMAIndicator(df["close"], 9).ema_indicator()
    df["ema21"] = EMAIndicator(df["close"], 21).ema_indicator()
    df["ema50"] = EMAIndicator(df["close"], 50).ema_indicator()
    df["ema200"] = EMAIndicator(df["close"], 200).ema_indicator()

    df["rsi"] = RSIIndicator(df["close"], 14).rsi()
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], 14).adx()

    macd = MACD(df["close"], 26, 12, 9)
    df["macd_line"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    df["sma20"] = SMAIndicator(df["close"], 20).sma_indicator()
    df["vol_sma20"] = df["volume"].rolling(20).mean()

    df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()
    df["atr_pct"] = df["atr14"] / df["close"] * 100

    df["cmf"] = ChaikinMoneyFlowIndicator(
        df["high"], df["low"], df["close"], df["volume"], 20
    ).chaikin_money_flow()

    bb = BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_high"] = bb.bollinger_hband()
    df["bb_low"] = bb.bollinger_lband()
    df["bb_width"] = (df["bb_high"] - df["bb_low"]) / df["close"]

    df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    df["obv_slope"] = df["obv"].diff()

    return df


# ============================================================
# 4) Daily Trend (BUY sinyali)
# ============================================================
def daily_long_condition(last):
    req = ["ema9","ema21","ema50","rsi","adx","macd_line","macd_signal","vol_sma20","cmf"]
    if any(pd.isna(last.get(k)) for k in req):
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
# 5) BullScore & Strateji Rozetleri
# ============================================================
def compute_bull_score(last):
    score = 0
    strong = []
    moderate = []

    # ⭐ STRONG
    if last["ema50"] > last["ema200"]:
        score += 3
        strong.append("Golden Cross")

    if last["bb_width"] < 0.06 and last["atr_pct"] < 3:
        score += 2
        strong.append("Sıkışma (BB+ATR)")

    if last["obv_slope"] > 0:
        score += 2
        strong.append("OBV ↑")

    # • MODERATE
    if last["macd_line"] > last["macd_signal"]:
        score += 1
        moderate.append("MACD Uptrend")

    if 45 < last["rsi"] < 60:
        score += 1
        moderate.append("RSI Zone")

    if last["cmf"] > 0:
        score += 1
        moderate.append("CMF+")

    if last["volume"] > (last["vol_sma20"] or 1) * 1.3:
        score += 1
        moderate.append("Volume Spike")

    if last["close"] > last["ema50"] and abs(last["close"] - last["sma20"]) / last["close"] < 0.02:
        score += 1
        moderate.append("Trend Bounce")

    # Rozet formatı
    strong_badges = ", ".join(["⭐ " + s for s in strong])
    mod_badges = ", ".join(["• " + s for s in moderate])

    strategies = ", ".join(filter(None, [strong_badges, mod_badges]))

    return score, strategies


# ============================================================
# 6) Entry/Stop/TP + Yüzdeler + R/R
# ============================================================
def calc_levels(last):
    entry = last["close"]
    atr = last["atr14"]
    if pd.isna(atr) or atr <= 0:
        return entry, None, None, None, None, None

    stop = entry - 1.5 * atr
    tp = entry + 2.5 * atr

    stop_pct = (entry - stop) / entry * 100
    tp_pct = (tp - entry) / entry * 100

    rr = tp_pct / stop_pct if stop_pct > 0 else None

    return entry, stop, tp, stop_pct, tp_pct, rr


# ============================================================
# 7) Hücre Renk Fonksiyonları
# ============================================================
def color_rsi(v):
    if 45 <= v <= 60: return "background:#064e3b;"   # yeşil
    if 35 <= v < 45 or 60 < v <= 70: return "background:#78350f;"  # sarı
    return "background:#7f1d1d;"  # kırmızı

def color_adx(v):
    if 20 <= v <= 40: return "background:#064e3b;"
    if 15 <= v < 20: return "background:#78350f;"
    return "background:#7f1d1d;"

def color_atr(v):
    if 1 <= v <= 5: return "background:#064e3b;"
    if 5 < v <= 10: return "background:#78350f;"
    return "background:#7f1d1d;"

def color_vol(v):
    if 0.9 <= v <= 1.4: return "background:#064e3b;"
    if 0.5 <= v < 0.9 or 1.4 < v <= 2: return "background:#78350f;"
    return "background:#7f1d1d;"

def color_cmf(v):
    if v > 0.05: return "background:#064e3b;"
    if 0 <= v <= 0.05: return "background:#78350f;"
    return "background:#7f1d1d;"

def color_rr(v):
    if v is None: return ""
    if v >= 1.5: return "background:#064e3b;"
    if 1 <= v < 1.5: return "background:#78350f;"
    return "background:#7f1d1d;"


# ============================================================
# 8) HTML Dashboard
# ============================================================
def generate_html(rows, output_path):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    h = [
        "<html><head><meta charset='UTF-8'>",
        "<title>Ultra Dashboard</title>",
        "<style>",
        "body{background:#020617;color:#e5e7eb;font-family:Arial;padding:20px;}",
        "table{width:100%;border-collapse:collapse;margin-top:20px;}",
        "th{background:#111827;padding:6px;font-size:12px;text-align:right;}",
        "td{padding:6px;font-size:12px;text-align:right;}",
        "tr:nth-child(even){background:#0f172a;}",
        "tr:nth-child(odd){background:#1f2937;}",
        ".sym{text-align:left;font-weight:bold;}",
        "</style></head><body>",
        "<h1 style='text-align:center;color:#facc15;'>Binance Spot – Ultra Dashboard</h1>",
        "<table>",
        "<tr>",
        "<th style='text-align:left'>Symbol</th>",
        "<th>Score</th>",
        "<th>Last</th>",
        "<th>Daily</th>",
        "<th>Entry</th>",
        "<th>Stop</th>",
        "<th>TP</th>",
        "<th>Stop%</th>",
        "<th>TP%</th>",
        "<th>R/R</th>",
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

        rsi_style = color_rsi(r["rsi"])
        adx_style = color_adx(r["adx"])
        atr_style = color_atr(r["atr_pct"])
        vol_style = color_vol(r["vol_ratio"])
        cmf_style = color_cmf(r["cmf"])
        rr_style = color_rr(r["rr"])

        daily_txt = "BUY" if r["daily"] else "-"

        h.append(
            "<tr>"
            f"<td class='sym'>{r['symbol']}</td>"
            f"<td>{r['score']}</td>"
            f"<td>{fmt(r['close'])}</td>"
            f"<td>{daily_txt}</td>"
            f"<td>{fmt(r['entry'])}</td>"
            f"<td>{fmt(r['stop'])}</td>"
            f"<td>{fmt(r['tp'])}</td>"
            f"<td>{fmt(r['stop_pct'],2)}</td>"
            f"<td>{fmt(r['tp_pct'],2)}</td>"
            f"<td style='{rr_style}'>{fmt(r['rr'],2)}</td>"
            f"<td style='{rsi_style}'>{fmt(r['rsi'],2)}</td>"
            f"<td style='{adx_style}'>{fmt(r['adx'],2)}</td>"
            f"<td style='{vol_style}'>{fmt(r['vol_ratio'],2)}</td>"
            f"<td style='{cmf_style}'>{fmt(r['cmf'],3)}</td>"
            f"<td style='{atr_style}'>{fmt(r['atr_pct'],2)}</td>"
            f"<td style='text-align:left'>{r['strategies']}</td>"
            "</tr>"
        )

    h.append("</table></body></html>")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(h))


# ============================================================
# 9) ANA AKIŞ
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

            score, strategies = compute_bull_score(last)
            entry, stop, tp, stop_pct, tp_pct, rr = calc_levels(last)
            daily = daily_long_condition(last)

            rows.append({
                "symbol": sym,
                "close": last["close"],
                "score": score,
                "daily": daily,
                "entry": entry,
                "stop": stop,
                "tp": tp,
                "stop_pct": stop_pct,
                "tp_pct": tp_pct,
                "rr": rr,
                "rsi": last["rsi"],
                "adx": last["adx"],
                "vol_ratio": last["volume"] / (last["vol_sma20"] or 1),
                "cmf": last["cmf"],
                "atr_pct": last["atr_pct"],
                "strategies": strategies,
            })

            time.sleep(0.03)
        except Exception:
            continue

    rows_sorted = sorted(rows, key=lambda r: (-r["score"], -int(r["daily"]), -r["rr"] if r["rr"] else 0))

    os.makedirs("public", exist_ok=True)
    generate_html(rows_sorted, "public/spot.html")
    print("✅ Ultra Dashboard üretildi")


if __name__ == "__main__":
    main()
