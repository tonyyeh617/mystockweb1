import sys
import time

from django.core.management.base import BaseCommand

from stocks.finmind import FinMindError, ensure_prices, ensure_stock_list
from stocks.models import Stock


class Command(BaseCommand):
    help = (
        "下載（或補齊）全部股票的每日價格資料。FinMind 未使用 token 時每小時有流量限制，"
        "一次執行若遇到限制會自動停止，可用排程（如 Windows 工作排程器）每隔一段時間重複執行以陸續補齊。"
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="最多處理幾檔股票（0 表示不限制）")
        parser.add_argument("--sleep", type=float, default=1.0, help="每檔股票之間的延遲秒數，避免觸發流量限制")

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

        stocks = list(Stock.objects.order_by("stock_id"))
        if options["limit"]:
            stocks = stocks[:options["limit"]]
        total = len(stocks)
        self.stdout.write(f"共 {total} 檔股票，開始下載...")

        updated = skipped = failed = 0
        for index, stock in enumerate(stocks, start=1):
            before = stock.last_fetched
            try:
                ensure_prices(stock)
            except FinMindError as e:
                failed += 1
                self._print_progress(index, total, stock, "失敗")
                self.stdout.write("")  # 結束進度列、換行
                self.stderr.write(self.style.WARNING(f"{stock} 失敗：{e}"))
                self.stdout.write(self.style.ERROR("可能已達 API 流量限制，停止本次執行，請稍後再執行一次以繼續補齊。"))
                break
            if stock.last_fetched == before:
                skipped += 1
                self._print_progress(index, total, stock, "略過（今日已抓過）")
            else:
                updated += 1
                self._print_progress(index, total, stock, "已更新")
            time.sleep(options["sleep"])
        else:
            self.stdout.write("")  # 正常跑完整個迴圈，結束進度列、換行

        self.stdout.write(self.style.SUCCESS(
            f"完成：更新 {updated} 檔、略過 {skipped} 檔（今日已抓過）、失敗 {failed} 檔"
        ))

