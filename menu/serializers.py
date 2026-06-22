from rest_framework import serializers
from .models import Order, Call, Menu, Category


class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = "__all__"


class CallSerializer(serializers.ModelSerializer):
    """Serializer for Call model"""
    class Meta:
        model = Call
        fields = "__all__"


class InitiateCallSerializer(serializers.Serializer):
    """Serializer for initiating phone calls"""
    order_id = serializers.IntegerField(required=False, help_text="Associated order ID")
    phone_number = serializers.CharField(max_length=50, help_text="Customer phone number")
    context = serializers.JSONField(required=False, help_text="Additional context for the call")


class ChatTokenSerializer(serializers.Serializer):
    """Serializer for generating browser chat tokens"""
    user_context = serializers.JSONField(required=False, help_text="User context data for the session")

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = "__all__"


class MenuSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True, required=False)
    category = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(),
        required=True,  # REQUIRED - must be sent from frontend
        allow_null=False,
        error_messages={
            'required': 'Category is required. Please select a category from the dropdown.',
            'does_not_exist': 'Selected category does not exist.',
            'incorrect_type': 'Category must be a valid category ID (integer).',
        }
    )
    
    class Meta:
        model = Menu
        fields = ['id', 'name', 'cost', 'category', 'category_name', 'created_at']
        read_only_fields = ['id', 'category_name', 'created_at']
        extra_kwargs = {
            'name': {
                'required': True,
                'error_messages': {'required': 'Item name is required.'}
            },
            'cost': {
                'required': True,
                'error_messages': {'required': 'Item cost is required.'}
            }
        }