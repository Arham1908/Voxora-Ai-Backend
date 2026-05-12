# analytics/views.py
from django.utils import timezone
from django.db.models import Sum, Count
from rest_framework.views import APIView
from rest_framework.response import Response
from menu.models import Order, Menu
from rest_framework.permissions import AllowAny

class order_stats(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        today = timezone.now().date()

        # Orders aggregation
        total_orders = Order.objects.count()
        total_revenue = Order.objects.aggregate(total=Sum('total_price'))['total'] or 0

        today_orders_qs = Order.objects.filter(created_at__date=today)
        today_orders = today_orders_qs.count()
        today_revenue = today_orders_qs.aggregate(total=Sum('total_price'))['total'] or 0

        # Menu aggregation
        total_menu_items = Menu.objects.count()
        total_calls = total_menu_items

        data = {
            "total_orders": total_orders,
            "total_revenue": total_revenue,
            "today_orders": today_orders,
            "today_revenue": today_revenue,
            "total_menu_items": total_menu_items,
            "total_calls": total_calls
        }

        return Response(data)
