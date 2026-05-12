from rest_framework import serializers
from django.contrib.auth import authenticate

from .models import User


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["email", "full_name", "password"]

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["email", "password"]

    def validate(self, attrs):
        email = attrs["email"]
        password = attrs["password"]
        user = authenticate(
            username=email,
            password=password,
        )
        if user is None:
            user = User.objects.filter(email__iexact=email).first()
            if user and user.check_password(password) and not user.is_active:
                raise serializers.ValidationError("Account is blocked")
            raise serializers.ValidationError("Invalid email or password")

        if not user.is_active:
            raise serializers.ValidationError("Account is blocked")

        attrs["user"] = user
        return attrs


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField()


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "is_active", "role", "created_at", "auth_provider"]
        read_only_fields = ["email", "role", "auth_provider", "created_at"]


class UpdateProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["avatar", "full_name"]


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, min_length=8)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Old password is not correct")
        return value

    def validate(self, attrs):
        if attrs["old_password"] == attrs["new_password"]:
            raise serializers.ValidationError(
                {"new_password": "New password must be different from old password"}
            )
        return attrs

    def save(self, **kwargs):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save()
        return user
