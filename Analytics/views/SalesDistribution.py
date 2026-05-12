# analytics/views.py
from django.utils import timezone
from datetime import timedelta
from collections import defaultdict
from rest_framework.views import APIView
from rest_framework.response import Response
from menu.models import Order
from rest_framework.permissions import AllowAny

class SalesDistribution(APIView):
    """
    API to get total quantity sold per menu item in the past 7 days
    """
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        today = timezone.now().date()
        seven_days_ago = today - timedelta(days=6)

        # Filter orders from last 7 days
        last_7_days_orders = Order.objects.filter(created_at__date__gte=seven_days_ago)

        # Dictionary to hold summary per item
        item_summary = defaultdict(int)

        for order in last_7_days_orders:
            for item in order.items:  # assuming each item is like {"name": "Pizza", "price": 500, "quantity": 2}
                name = item.get("name")
                quantity = item.get("quantity", 1)

                item_summary[name] += quantity

        # Convert to desired format
        result = [{"name": name, "value": quantity} for name, quantity in item_summary.items()]

        return Response({"items_sold_history": result})
