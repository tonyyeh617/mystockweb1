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
    k = d = 50.0  # KD 慣例初始值
    for i, p in enumerate(prices):
        k_val = d_val = None
        if i >= 8:
            window = prices[i - 8:i + 1]
            hi, lo = max(w.high for w in window), min(w.low for w in window)
            rsv = (p.close - lo) / (hi - lo) * 100 if hi != lo else 50.0
            k = k * 2 / 3 + rsv / 3
            d = d * 2 / 3 + k / 3
            k_val, d_val = round(k, 2), round(d, 2)
        ma20 = sum(closes[i - 19:i + 1]) / 20 if i >= 19 else None
        ma60 = sum(closes[i - 59:i + 1]) / 60 if i >= 59 else None
        out.append({
            "date": p.date.isoformat(),
            "open": p.open, "high": p.high, "low": p.low, "close": p.close,
            "volume": p.volume,
            "vwap": round((p.high + p.low + p.close) / 3, 2),
            "ma20": round(ma20, 2) if ma20 is not None else None,
            "ma60": round(ma60, 2) if ma60 is not None else None,
            "k": k_val, "d": d_val,
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


def volume_history_window():
    """回傳 (start, end)：近3年區間，自一個月前往回推算。"""
    end = date.today() - timedelta(days=VOLUME_HISTORY_END_LAG_DAYS)
    start = end - timedelta(days=365 * VOLUME_HISTORY_YEARS)
    return start, end


VOLUME_COVERAGE_TOLERANCE_DAYS = 10  # 容許假日/停牌造成的首尾日期落差
VOLUME_MARKER_PREFIX = "volume_covered:"


def plan_volume_fetch(stock, start, end):
    """依資料庫現有紀錄決定需下載的起日；回傳 None 表示整段已抓過，不需呼叫 API（不耗 token）。

    - 已有標記（曾完整抓過區間，如新上市股票）或最早資料已涵蓋區間起點：只需補最後一筆之後的資料。
    - 否則（尚未抓過）：下載整段區間。
    """
    from .models import FetchCheckpoint

    tol = timedelta(days=VOLUME_COVERAGE_TOLERANCE_DAYS)
    first = stock.prices.order_by("date").first()
    last = stock.prices.order_by("-date").first()
    marker = FetchCheckpoint.objects.filter(key=VOLUME_MARKER_PREFIX + stock.stock_id).first()
    marker_end = None
    if marker and marker.last_stock_id:
        try:
            marker_end = date.fromisoformat(marker.last_stock_id)
        except ValueError:
            pass

    start_covered = (first is not None and first.date <= start + tol) or marker_end is not None
    if not start_covered or last is None:
        return start
    latest = max(last.date, marker_end) if marker_end else last.date
    if latest >= end - tol:
        return None
    return latest + timedelta(days=1)


def mark_volume_covered(stock, end):
    from .models import FetchCheckpoint

    FetchCheckpoint.objects.update_or_create(
        key=VOLUME_MARKER_PREFIX + stock.stock_id,
        defaults={"last_stock_id": end.isoformat()},
    )


# ---- 選股：近三年錨定 VWAP 上下震盪，且近六個月位於低檔 ----
VWAP_BAND = 0.01  # 收盤價須偏離錨定VWAP超過 1% 才算「站上／跌破」，過濾微小交叉
MIN_YEAR_CROSSINGS = 3  # 每一年度至少穿越錨定VWAP的次數
MIN_YEAR_DEVIATION = 0.03  # 每一年度須同時有高於、低於錨定VWAP至少 3% 的時候
MIN_SIDE_RATIO = 0.2  # 每一年度在VWAP上方（或下方）的天數占比須介於 20%~80%，排除單邊走勢
LOW_WINDOW_DAYS = 183  # 近六個月
LOW_POSITION_MAX = 0.2  # 最新收盤須位於六個月價格區間底部 20%
MIN_TRADING_DAYS = 600  # 三年約 720 個交易日，不足太多表示資料不完整


def _count_crossings(closes, vwaps):
    """以 ±VWAP_BAND 帶寬做遲滯判斷，回傳（有效穿越次數、最大正偏離、最大負偏離、在VWAP上方天數占比）。"""
    state = 0  # 1：上方，-1：下方
    crossings = 0
    max_up = max_down = 0.0
    above = 0
    for c, v in zip(closes, vwaps):
        dev = c / v - 1
        max_up, max_down = max(max_up, dev), min(max_down, dev)
        above += c >= v
        new_state = 1 if dev > VWAP_BAND else -1 if dev < -VWAP_BAND else 0
        if new_state and new_state != state:
            if state:
                crossings += 1
            state = new_state
    return crossings, max_up, max_down, above / len(closes)


def evaluate_vwap_oscillation(rows, start):
    """rows：(date, high, low, close, volume) 依日期排序。符合條件回傳評分字典，否則回傳 None。"""
    rows = [r for r in rows if r[0] >= start and r[4] > 0 and r[1] > 0 and r[2] > 0 and r[3] > 0]
    if len(rows) < MIN_TRADING_DAYS:
        return None

    pv = v = 0.0
    vwaps = []
    for _, hi, lo, close, vol in rows:
        pv += (hi + lo + close) / 3 * vol
        v += vol
        vwaps.append(pv / v)
    closes = [r[3] for r in rows]
    dates = [r[0] for r in rows]

    # 三個年度區段；最後一段延伸到最新交易日
    cuts = [start + timedelta(days=365 * n) for n in (1, 2)]
    segments = [[], [], []]
    for i, d in enumerate(dates):
        segments[0 if d <= cuts[0] else 1 if d <= cuts[1] else 2].append(i)

    year_stats = []
    for seg in segments:
        if len(seg) < 100:
            return None
        cr, up, down, above_ratio = _count_crossings([closes[i] for i in seg], [vwaps[i] for i in seg])
        if (cr < MIN_YEAR_CROSSINGS or up < MIN_YEAR_DEVIATION or -down < MIN_YEAR_DEVIATION
                or not MIN_SIDE_RATIO <= above_ratio <= 1 - MIN_SIDE_RATIO):
            return None
        year_stats.append((cr, up - down, above_ratio))

    # 近六個月價格區間的位置（0 = 最低、1 = 最高）
    latest = rows[-1]
    recent = [r for r in rows if r[0] > latest[0] - timedelta(days=LOW_WINDOW_DAYS)]
    lo6, hi6 = min(r[2] for r in recent), max(r[1] for r in recent)
    if hi6 <= lo6:
        return None
    position = (latest[3] - lo6) / (hi6 - lo6)
    if position > LOW_POSITION_MAX:
        return None

    avg_cross = sum(s[0] for s in year_stats) / 3
    avg_amp = sum(s[1] for s in year_stats) / 3
    balance = sum(1 - abs(2 * s[2] - 1) for s in year_stats) / 3
    score = (
        min(avg_cross / 8, 1) * 40  # 穿越頻率
        + min(avg_amp / 0.20, 1) * 20  # 波動幅度
        + balance * 20  # 上下方停留時間均衡
        + (1 - position / LOW_POSITION_MAX) * 20  # 越接近六個月低點越高
    )
    return {
        "crossings": [s[0] for s in year_stats],
        "avg_amp": round(avg_amp * 100, 1),
        "position": round(position * 100, 1),
        "close": latest[3],
        "anchored_vwap": round(vwaps[-1], 2),
        "latest_date": latest[0],
        "score": round(score, 1),
    }


def find_vwap_oscillation_candidates(limit=20):
    """找出近三年每年都明顯上下穿越錨定VWAP、且最新收盤位於近六個月區間底部 20% 的股票，依評分取前 limit 檔。"""
    start, end = volume_history_window()
    tol = timedelta(days=VOLUME_COVERAGE_TOLERANCE_DAYS)
    candidates = []
    stocks = Stock.objects.filter(prices__date__gte=start, prices__date__lte=end).distinct()
    for stock in stocks:
        rows = list(stock.prices.filter(date__gte=start).order_by("date")
                    .values_list("date", "high", "low", "close", "volume"))
        if not rows or rows[0][0] > start + tol or rows[-1][0] < end - tol:
            continue  # 資料未涵蓋完整三年區間
        result = evaluate_vwap_oscillation(rows, start)
        if result:
            result["stock"] = stock
            candidates.append(result)
    candidates.sort(key=lambda c: -c["score"])
    return candidates[:limit]



