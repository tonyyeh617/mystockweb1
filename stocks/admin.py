from django.contrib import admin

from .models import DailyPrice, Dividend, Favorite, Stock

admin.site.register(Stock)
admin.site.register(DailyPrice)
admin.site.register(Dividend)
admin.site.register(Favorite)
