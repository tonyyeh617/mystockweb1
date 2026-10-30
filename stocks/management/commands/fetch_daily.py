import sys
import time
from datetime import date

from django.core.management.base import BaseCommand

from stocks.finmind import (
    FinMindError, ensure_dividends, ensure_prices, ensure_stock_list,
    fetch_price_range, mark_volume_covered, plan_volume_fetch, volume_history_window,
)
from stocks.models import Stock


class Command(BaseCommand):
    help = (
        "合併每日例行任務：依優先序（股價 > 成交量歷史 > 股利）共用同一包 FinMind 流量額度"
        "（未使用 token 時每小時 300 次），額度用完就停止，下次排程（建議每小時）會自動接續"
        "（皆依資料庫現況判斷是否需要補資料，不需額外記錄進度）。"
        "用來取代同時排程 fetch_all_prices / fetch_volume_history / fetch_dividends 三支指令，"
        "避免因排程時段重疊而互搶額度；三支原始指令仍保留，可單獨手動執行除錯，"
        "股利「整段重抓最新年度」（--force）請仍用 fetch_dividends 手動執行。"
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=300, help="本次最多呼叫幾次 FinMind API（即流量額度，預設 300）")
        parser.add_argument("--sleep", type=float, default=1.0, help="每次 API 呼叫之間的延遲秒數，避免觸發流量限制")

    def _print_progress(self, index, total, stock, status):
        width = 30
        filled = int(width * index / total) if total else width
        bar = "#" * filled + "-" * (width - filled)
        pct = index / total * 100 if total else 100
        line = f"\r[{bar}] {index}/{total} ({pct:5.1f}%) {stock.stock_id} {stock.name} - {status}"
        self.stdout.write(line.ljust(90), ending="")
        sys.stdout.flush()

    def handle(self, *args, **options):
        try:
            ensure_stock_list()
        except FinMindError as e:
            self.stderr.write(self.style.ERROR(f"無法取得股票清單：{e}"))
            return

        budget = options["limit"]
        sleep_sec = options["sleep"]
        today = date.today()
        vol_start, vol_end = volume_history_window()

        stocks = list(Stock.objects.order_by("stock_id"))
        total = len(stocks)
        self.stdout.write(f"共 {total} 檔股票，本次流量額度 {budget} 次...")

        price_done = volume_done = dividend_done = 0
        stopped_early = False

        for index, stock in enumerate(stocks, start=1):
            if budget <= 0:
                stopped_early = True
                break

            status_parts = []
            try:
                # 1) 股價：每天都要更新，優先權最高
                if stock.last_fetched != today:
                    ensure_prices(stock)
                    price_done += 1
                    budget -= 1
                    status_parts.append("股價已更新")
                    time.sleep(sleep_sec)

                # 2) 成交量歷史：budget 還夠才補
                if budget > 0:
                    fetch_start = plan_volume_fetch(stock, vol_start, vol_end)
                    if fetch_start:
                        fetch_price_range(stock, fetch_start, vol_end)
                        mark_volume_covered(stock, vol_end)
                        volume_done += 1
                        budget -= 1
                        status_parts.append("成交量已補")
                        time.sleep(sleep_sec)

                # 3) 股利：budget 還夠、且尚未抓過才補（整段重抓最新年度請用 fetch_dividends --force）
                if budget > 0 and not stock.dividends.exists():
                    ensure_dividends(stock)
                    dividend_done += 1
                    budget -= 1
                    status_parts.append("股利已補")
                    time.sleep(sleep_sec)
            except FinMindError as e:
                self._print_progress(index, total, stock, "失敗")
                self.stdout.write("")  # 結束進度列、換行
                self.stderr.write(self.style.WARNING(f"{stock} 失敗：{e}"))
                self.stdout.write(self.style.ERROR(
                    "可能已達 API 流量限制，已停止；下次排程會依資料庫現況自動繼續未完成的部分。"
                ))
                stopped_early = True
                break

            self._print_progress(index, total, stock, "、".join(status_parts) or "略過（皆已是最新）")
        else:
            self.stdout.write("")  # 正常跑完整個迴圈，結束進度列、換行

        if not stopped_early:
            self.stdout.write(self.style.SUCCESS("全部股票本輪皆已檢查完成。"))

        self.stdout.write(self.style.SUCCESS(
            f"完成：股價更新 {price_done} 檔、成交量補齊 {volume_done} 檔、股利補齊 {dividend_done} 檔"
            f"（剩餘額度 {budget}）"
        ))
