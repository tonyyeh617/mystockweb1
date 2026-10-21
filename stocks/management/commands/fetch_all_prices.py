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

    def handle(self, *args, **options):
        try:
            ensure_stock_list()
        except FinMindError as e:
            self.stderr.write(self.style.ERROR(f"無法取得股票清單：{e}"))
            return

        stocks = Stock.objects.order_by("stock_id")
        if options["limit"]:
            stocks = stocks[:options["limit"]]

        updated = skipped = failed = 0
        for stock in stocks:
            before = stock.last_fetched
            try:
                ensure_prices(stock)
            except FinMindError as e:
                failed += 1
                self.stderr.write(self.style.WARNING(f"{stock} 失敗：{e}"))
                self.stdout.write(self.style.ERROR("可能已達 API 流量限制，停止本次執行，請稍後再執行一次以繼續補齊。"))
                break
            if stock.last_fetched == before:
                skipped += 1
            else:
                updated += 1
                self.stdout.write(f"已更新 {stock}")
            time.sleep(options["sleep"])

        self.stdout.write(self.style.SUCCESS(
            f"完成：更新 {updated} 檔、略過 {skipped} 檔（今日已抓過）、失敗 {failed} 檔"
        ))
