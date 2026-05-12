# analytics/views.py
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum
from rest_framework.views import APIView
from rest_framework.response import Response
from menu.models import Order
from rest_framework.permissions import AllowAny

class RevenuePerformance(APIView):
    """
    API to get number of orders and total revenue for each of the last 7 days
    """
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        today = timezone.now().date()
        last_7_days = [today - timedelta(days=i) for i in range(6, -1, -1)]  # oldest -> newest

        result = []
        for day in last_7_days:
            qs = Order.objects.filter(created_at__date=day)
            orders_count = qs.count()
            revenue = qs.aggregate(total=Sum('total_price'))['total'] or 0

            result.append({
                "date": day.strftime("%Y-%m-%d"),
                "orders_count": orders_count,
                "revenue": revenue
            })

        return Response({"last_7_days": result})
