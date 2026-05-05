from rest_framework import serializers
from django.contrib.auth import authenticate
from .models import User

class RegisterSerializer(serializers.ModelSerializer):
    password= serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields= ['email','full_name','password']

    def validate(self,attrs):
        return attrs
    
    def create(self,validated_data):
        return User.objects.create_user(**validated_data)

class LoginSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    class Meta:
        model =User
        fields =['email','password']
    def validate(self,attrs):
        user = authenticate(username=attrs['email'],password=attrs['password'])
        if not user:
            raise serializers.ValidationError('Invalid email or password')
        if not user.is_active:
            raise serializers.ValidationError('Account is Blocked')
        attrs['user'] =user
        return attrs

class GoogleAuthSerializer(serializers.ModelSerializer):
    id_token =serializers.CharField()

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields =['id','email','is_active','role','created_at','auth_provider']
        read_only_fields =['email','role','auth_provider','created_at']

class UpdateProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields =['avatar','full_name']

class ChangePasswordSerializer(serializers.ModelSerializer):
    old_password =serializers.CharField(write_only=True)
    new_password =serializers.CharField(write_only=True)

    def validate(self,value):
        user =self.context['request'].user
        if not  user.check_password(value):
            raise serializers.ValidationError('Old Password is not correct')
        return value
    def save(self):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save()