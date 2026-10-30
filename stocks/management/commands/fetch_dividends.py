import sys

from django.core.management.base import BaseCommand

from stocks.finmind import FinMindError, ensure_dividends, ensure_stock_list
from stocks.models import FetchCheckpoint, Stock

CHECKPOINT_KEY = "dividend_history"


class Command(BaseCommand):
    help = (
        "下載全部股票的股利政策（股息）歷史資料，供「股息排行」頁面使用。"
        "FinMind 未使用 token 時每小時有流量限制，遇到限制會記錄目前進度並停止，"
        "下次執行會從上次中斷的股票繼續抓取。此指令設計為自行手動執行，之後可改排程執行。"
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="本次最多處理幾檔股票（0 表示不限制）")
        parser.add_argument("--reset", action="store_true", help="忽略先前進度，從頭開始")
        parser.add_argument("--force", action="store_true", help="即使已有資料也重新抓取（更新最新年度）")

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

        checkpoint, _ = FetchCheckpoint.objects.get_or_create(key=CHECKPOINT_KEY)
        if options["reset"]:
            checkpoint.last_stock_id = ""
            checkpoint.save(update_fields=["last_stock_id"])

        stocks_qs = Stock.objects.order_by("stock_id")
        if checkpoint.last_stock_id:
            stocks_qs = stocks_qs.filter(stock_id__gt=checkpoint.last_stock_id)
            self.stdout.write(f"接續上次進度，從 {checkpoint.last_stock_id} 之後開始")
        stocks = list(stocks_qs)
        if options["limit"]:
            stocks = stocks[:options["limit"]]
        total = len(stocks)
        self.stdout.write(f"本次預計處理 {total} 檔股票...")

        done = failed = skipped = 0
        for index, stock in enumerate(stocks, start=1):
            if not options["force"] and stock.dividends.exists():
                skipped += 1
                self._print_progress(index, total, stock, "略過（已有資料）")
                checkpoint.last_stock_id = stock.stock_id
                checkpoint.save(update_fields=["last_stock_id", "updated_at"])
                continue
            try:
                ensure_dividends(stock, force=options["force"])
            except FinMindError as e:
                self._print_progress(index, total, stock, "失敗")
                self.stdout.write("")  # 結束進度列、換行
                self.stderr.write(self.style.WARNING(f"{stock} 失敗：{e}"))
                self.stdout.write(self.style.ERROR(
                    "可能已達 API 流量限制，已記錄進度並停止，下次執行會從此處繼續抓取。"
                ))
                failed += 1
                break
            checkpoint.last_stock_id = stock.stock_id
            checkpoint.save(update_fields=["last_stock_id", "updated_at"])
            done += 1
            self._print_progress(index, total, stock, "已下載")
        else:
            self.stdout.write("")  # 正常跑完整個迴圈，結束進度列、換行
            if total:
                self.stdout.write(self.style.SUCCESS("全部股票皆已處理完成，進度已重置，下次會重新開始。"))
            checkpoint.last_stock_id = ""
            checkpoint.save(update_fields=["last_stock_id", "updated_at"])

        self.stdout.write(self.style.SUCCESS(f"完成：成功 {done} 檔、略過 {skipped} 檔（已有資料）、失敗 {failed} 檔"))
