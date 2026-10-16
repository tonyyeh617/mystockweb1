from django.contrib import admin

from .models import DailyPrice, Favorite, Stock

admin.site.register(Stock)
admin.site.register(DailyPrice)
admin.site.register(Favorite)
