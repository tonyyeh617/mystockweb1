from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .finmind import FinMindError, build_series, ensure_prices, ensure_stock_list, find_vwap_oscillation_candidates
from .forms import SignUpForm
from .models import Favorite, Stock


def signup(request):
    if request.method == "POST":
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "註冊成功，歡迎使用！!!")
            return redirect("home")
    else:
        form = SignUpForm()
    return render(request, "registration/signup.html", {"form": form})


@login_required
def home(request):
    q = request.GET.get("q", "").strip()
    results = []
    if q:
        try:
            ensure_stock_list()
        except FinMindError as e:
            messages.error(request, str(e))
        results = Stock.objects.filter(Q(stock_id__icontains=q) | Q(name__icontains=q)).order_by("stock_id")[:50]
    fav_ids = set(request.user.favorites.values_list("stock_id", flat=True))
    return render(request, "stocks/home.html", {"q": q, "results": results, "fav_ids": fav_ids})


@login_required
def stock_detail(request, stock_id):
    try:
        ensure_stock_list()
    except FinMindError as e:
        messages.error(request, str(e))
    stock = get_object_or_404(Stock, stock_id=stock_id)
    try:
        ensure_prices(stock)
    except FinMindError as e:
        messages.error(request, str(e))
    is_fav = Favorite.objects.filter(user=request.user, stock=stock).exists()
    return render(request, "stocks/detail.html", {
        "stock": stock,
        "series": build_series(stock),
        "is_fav": is_fav,
    })


@login_required
@require_POST
def toggle_favorite(request, stock_id):
    stock = get_object_or_404(Stock, stock_id=stock_id)
    fav, created = Favorite.objects.get_or_create(user=request.user, stock=stock)
    if created:
        messages.success(request, f"已將 {stock} 加入我的最愛")
    else:
        fav.delete()
        messages.info(request, f"已將 {stock} 移出我的最愛")
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("stock_detail", stock_id=stock_id)


@login_required
@require_POST
def refresh_price(request, stock_id):
    stock = get_object_or_404(Stock, stock_id=stock_id)
    try:
        ensure_prices(stock, force=True)
        messages.success(request, f"已更新 {stock} 的最新資料")
    except FinMindError as e:
        messages.error(request, str(e))
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("stock_detail", stock_id=stock_id)


@login_required
def screener(request):
    candidates = find_vwap_oscillation_candidates(limit=20)
    fav_ids = set(request.user.favorites.values_list("stock_id", flat=True))
    return render(request, "stocks/screener.html", {"candidates": candidates, "fav_ids": fav_ids})


@login_required
def favorites(request):
    favs = request.user.favorites.select_related("stock").order_by("stock__stock_id")
    rows = []
    for f in favs:
        last = f.stock.prices.order_by("-date").first()
        rows.append({"stock": f.stock, "last": last})
    return render(request, "stocks/favorites.html", {"rows": rows})
