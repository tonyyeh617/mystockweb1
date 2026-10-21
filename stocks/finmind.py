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


def fetch_price_range(stock, start, end):
    """直接向 FinMind 下載指定區間的價格並覆蓋寫入，不影響 ensure_prices 的每日快取旗標。"""
    data = _get({
        "dataset": "TaiwanStockPrice",
        "data_id": stock.stock_id,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
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
        DailyPrice.objects.bulk_create(
            objs, update_conflicts=True,
            update_fields=["open", "high", "low", "close", "volume"],
            unique_fields=["stock", "date"],
        )
    return len(objs)


VOLUME_HISTORY_YEARS = 3
VOLUME_HISTORY_END_LAG_DAYS = 30  # 從一個月前開始往回算，避免當月資料不完整
VOLUME_DIP_RATIO = 1.5  # 三年中最高量須達今年量的幾倍，才算「高低落差明顯」


def volume_history_window():
    """回傳 (start, end)：近3年區間，自一個月前往回推算。"""
    end = date.today() - timedelta(days=VOLUME_HISTORY_END_LAG_DAYS)
    start = end - timedelta(days=365 * VOLUME_HISTORY_YEARS)
    return start, end


def find_volume_dip_candidates(limit=20):
    """在已快取近3年資料的股票中，找出「今年成交量為三年最低、且三年間有明顯高低落差」的候選股。"""
    start, end = volume_history_window()
    year2_cut = end - timedelta(days=365)
    year1_cut = end - timedelta(days=365 * 2)

    candidates = []
    stocks = Stock.objects.filter(prices__date__gte=start, prices__date__lte=end).distinct()
    for stock in stocks:
        prices = list(stock.prices.filter(date__gte=start, date__lte=end))
        if len(prices) < 400:  # 資料不足三年的交易日數就略過（約每年200個交易日）
            continue

        vol_y1 = sum(p.volume for p in prices if p.date <= year1_cut)
        vol_y2 = sum(p.volume for p in prices if year1_cut < p.date <= year2_cut)
        vol_y3 = sum(p.volume for p in prices if p.date > year2_cut)  # 今年
        years_vol = [vol_y1, vol_y2, vol_y3]
        if min(years_vol) <= 0 or vol_y3 != min(years_vol):
            continue  # 今年不是三年中成交量最低的一年

        ratio = max(years_vol) / vol_y3
        if ratio < VOLUME_DIP_RATIO:
            continue  # 高低落差不夠明顯

        candidates.append({
            "stock": stock,
            "vol_y1": vol_y1,
            "vol_y2": vol_y2,
            "vol_y3": vol_y3,
            "ratio": round(ratio, 2),
        })

    candidates.sort(key=lambda c: -c["ratio"])
    return candidates[:limit]


