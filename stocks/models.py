from django.conf import settings
from django.db import models


class Stock(models.Model):
    stock_id = models.CharField("代號", max_length=10, primary_key=True)
    name = models.CharField("名稱", max_length=50)
    industry = models.CharField("產業", max_length=50, blank=True)
    market = models.CharField("市場", max_length=10, blank=True)
    last_fetched = models.DateField("最後抓取日", null=True, blank=True)

    def __str__(self):
        return f"{self.stock_id} {self.name}"


class DailyPrice(models.Model):
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE, related_name="prices")
    date = models.DateField()
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.BigIntegerField()

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["stock", "date"], name="uniq_stock_date")]


class Favorite(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorites")
    stock = models.ForeignKey(Stock, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "stock"], name="uniq_user_stock")]
