import sys
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from stocks.finmind import FinMindError, ensure_prices, ensure_stock_list
from stocks.models import Stock


class Command(BaseCommand):
    help = (
        "下載（或補齊）全部股票的每日價格資料。FinMind 未使用 token 時每小時有流量限制，"
        "一次執行若遇到限制會自動停止，可用排程（如 Windows 工作排程器）每隔一段時間重複執行以陸續補齊。"
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=300, help="本次最多實際下載幾檔股票（已是最新而略過的不計入，0 表示不限制）")
        parser.add_argument("--sleep", type=float, default=1.0, help="每檔股票之間的延遲秒數，避免觸發流量限制")

    def _print_progress(self, index, total, stock, status):
        width = 30
        filled = int(width * index / total) if total else width
        bar = "#" * filled + "-" * (width - filled)
        pct = index / total * 100 if total else 100
        line = f"\r[{bar}] {index}/{total} ({pct:5.1f}%) {stock.stock_id} {stock.name} - {status}"
        self.stdout.write(line.ljust(90), ending="")
        sys.stdout.flush()

    def _print_finished_at(self):
        finished_at = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
        self.stdout.write(f"執行結束時間：{finished_at}")

    def handle(self, *args, **options):
        try:
            ensure_stock_list()
        except FinMindError as e:
            self.stderr.write(self.style.ERROR(f"無法取得股票清單：{e}"))
            self._print_finished_at()
            return

        stocks = list(Stock.objects.order_by("stock_id"))
        limit = options["limit"]
        total = len(stocks)
        self.stdout.write(f"共 {total} 檔股票，本次最多下載 {limit or '不限'} 檔...")

        updated = skipped = failed = 0
        for index, stock in enumerate(stocks, start=1):
            if limit and updated >= limit:
                self.stdout.write("")
                self.stdout.write(self.style.WARNING(f"已達本次下載上限 {limit} 檔，停止；下次執行會繼續。"))
                break
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
        self._print_finished_at()
