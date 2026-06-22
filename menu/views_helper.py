from rest_framework.decorators import api_view,permission_classes
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import csrf_exempt
from .serializers import OrderSerializer, InitiateCallSerializer, ChatTokenSerializer, CallSerializer, MenuSerializer, CategorySerializer
from .models import Order, Call, Menu, Category
from .services import ElevenLabsService
from kfc_api.pagination import paginate_queryset
import uuid
from rest_framework.permissions import AllowAny


# ─────────────────────────────────────────────────────────────────────────────
# API HELPER ENDPOINT - Shows required format and debugging info
# ─────────────────────────────────────────────────────────────────────────────

@csrf_exempt
@api_view(['GET'])
@permission_classes([AllowAny])
def menu_api_help(request):
    """
    API Helper - Shows the required format for creating menu items
    GET /menu/help/
    """
    all_categories = Category.objects.all()
    category_list = [
        {
            "id": cat.id,
            "name": cat.name
        }
        for cat in all_categories
    ]
    
    return Response({
        "message": "Menu Item Creation API - Required Format",
        "endpoint": "POST /menu/",
        "required_fields": {
            "name": {
                "type": "string",
                "description": "Item name",
                "example": "Zinger Burger"
            },
            "cost": {
                "type": "integer",
                "description": "Price",
                "example": 500
            },
            "category": {
                "type": "integer",
                "description": "Category ID - REQUIRED - Must be selected from dropdown",
                "example": 4,
                "error_if_missing": "Category is required. Please select a category from the dropdown."
            }
        },
        "example_request": {
            "name": "Zinger Burger",
            "cost": 500,
            "category": 4
        },
        "available_categories": category_list,
        "note": "The 'category' field MUST be included in the request. Frontend should get this from the dropdown selection."
    })
