"""FinMind API 存取與資料庫快取（不使用 token）。"""
from datetime import date, datetime, timedelta

import requests

from .models import DailyPrice, Stock

API_URL = "https://api.finmindtrade.com/api/v4/data"
YEARS = 5
WARMUP_DAYS = 120  # 多抓一段資料讓 MA60 在 5 年起點就有值


class FinMindError(Exception):
    pass


def _get(params):
    try:
        resp = requests.get(API_URL, params=params, timeout=30)
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise FinMindError(f"連線 FinMind 失敗：{e}")
    if payload.get("status") != 200:
        raise FinMindError(f"FinMind 回傳錯誤：{payload.get('msg')}（未使用 token 時每小時有次數限制）")
    return payload.get("data", [])


def ensure_stock_list():
    """股票清單若尚未載入，從 TaiwanStockInfo 抓取一次。"""
    if Stock.objects.exists():
        return
    rows = {}
    for r in _get({"dataset": "TaiwanStockInfo"}):
        rows[r["stock_id"]] = Stock(
            stock_id=r["stock_id"],
            name=r["stock_name"],
            industry=r.get("industry_category", ""),
            market=r.get("type", ""),
        )
    Stock.objects.bulk_create(rows.values(), ignore_conflicts=True)


def ensure_prices(stock, force=False):
    """當天已抓過就直接用資料庫；force=True 時無視快取，重抓最後一天起的資料並覆蓋。"""
    today = date.today()
    if not force and stock.last_fetched == today:
        return
    last = stock.prices.order_by("-date").first()
    if last:
        start = last.date if force else last.date + timedelta(days=1)
    else:
        start = today - timedelta(days=365 * YEARS + WARMUP_DAYS)

    if start <= today:
        data = _get({
            "dataset": "TaiwanStockPrice",
            "data_id": stock.stock_id,
            "start_date": start.isoformat(),
            "end_date": today.isoformat(),
        })
        objs = [
            DailyPrice(
                stock=stock,
                date=datetime.strptime(r["date"], "%Y-%m-%d").date(),
                open=r["open"], high=r["max"], low=r["min"], close=r["close"],
                volume=r["Trading_Volume"],
            )
            for r in data
            if r["Trading_Volume"] > 0  # 排除停牌日
        ]
        if objs:
            if force:
                # 強制更新時覆蓋已存在的當天資料，而非略過
                DailyPrice.objects.bulk_create(
                    objs, update_conflicts=True,
                    update_fields=["open", "high", "low", "close", "volume"],
                    unique_fields=["stock", "date"],
                )
            else:
                DailyPrice.objects.bulk_create(objs, ignore_conflicts=True)

    stock.last_fetched = today
    stock.save(update_fields=["last_fetched"])


def build_series(stock):
    """回傳每日資料：典型價 VWAP 與 MA20 / MA60（以全部歷史計算）。"""
    prices = list(stock.prices.order_by("date"))
    closes = [p.close for p in prices]
    out = []
    for i, p in enumerate(prices):
        ma20 = sum(closes[i - 19:i + 1]) / 20 if i >= 19 else None
        ma60 = sum(closes[i - 59:i + 1]) / 60 if i >= 59 else None
        out.append({
            "date": p.date.isoformat(),
            "open": p.open, "high": p.high, "low": p.low, "close": p.close,
            "volume": p.volume,
            "vwap": round((p.high + p.low + p.close) / 3, 2),
            "ma20": round(ma20, 2) if ma20 is not None else None,
            "ma60": round(ma60, 2) if ma60 is not None else None,
        })
    cutoff = (date.today() - timedelta(days=365 * YEARS)).isoformat()
    return [r for r in out if r["date"] >= cutoff]


BREAKOUT_WINDOW = 120  # 高低點觀察區間（交易日）
BREAKOUT_LOOKBACK = 10  # 往前找幾天內的 VWAP 向上突破
VOLUME_MA = 20  # 成交量均量天數


def find_breakout_candidates(limit=20):
    """在已快取價格資料的股票中，篩選「深跌後止穩、站上VWAP」的候選股，回傳前 limit 名。

    條件：1) 現價較120日高點回落 >=40%；2) 現價位於120日區間底部20%；
    3) 近期出現向上突破VWAP；4) 突破後至今收盤都維持在VWAP之上；
    5) 突破日成交量放大（>=20日均量1.2倍）且120日低點是近期（60日內）形成。
    """
    need = BREAKOUT_WINDOW + VOLUME_MA + BREAKOUT_LOOKBACK
    candidates = []
    for stock in Stock.objects.filter(prices__isnull=False).distinct():
        prices = list(stock.prices.order_by("-date")[:need])
        prices.reverse()  # 轉成日期由舊到新
        if len(prices) < BREAKOUT_WINDOW + 1:
            continue

        window = prices[-BREAKOUT_WINDOW:]
        high120 = max(p.high for p in window)
        low120 = min(window, key=lambda p: p.low)
        if high120 <= 0 or high120 == low120.low:
            continue

        current = window[-1]
        drop_pct = (high120 - current.close) / high120 * 100
        pos_pct = (current.close - low120.low) / (high120 - low120.low) * 100
        if drop_pct < 40 or pos_pct > 20:
            continue

        closes = [p.close for p in prices]
        vwaps = [(p.high + p.low + p.close) / 3 for p in prices]

        breakout_idx = None
        earliest = max(len(prices) - 1 - BREAKOUT_LOOKBACK, 0)
        for i in range(len(prices) - 1, earliest, -1):
            if closes[i] > vwaps[i] and closes[i - 1] <= vwaps[i - 1]:
                breakout_idx = i
                break
        if breakout_idx is None:
            continue
        if any(closes[i] <= vwaps[i] for i in range(breakout_idx, len(prices))):
            continue  # 突破後曾跌破VWAP，不符合「維持在上方」

        vol_window = prices[max(breakout_idx - VOLUME_MA, 0):breakout_idx]
        vol_ma20 = sum(p.volume for p in vol_window) / len(vol_window) if vol_window else 0
        if not vol_ma20 or prices[breakout_idx].volume < vol_ma20 * 1.2:
            continue
        if (current.date - low120.date).days > 60:
            continue  # 120日低點非近期形成

        candidates.append({
            "stock": stock,
            "close": current.close,
            "high120": high120,
            "low120": low120.low,
            "drop_pct": round(drop_pct, 1),
            "pos_pct": round(pos_pct, 1),
            "breakout_date": prices[breakout_idx].date,
            "volume_ratio": round(prices[breakout_idx].volume / vol_ma20, 2),
        })

    candidates.sort(key=lambda c: (-c["drop_pct"], c["pos_pct"]))
    return candidates[:limit]
