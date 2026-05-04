from django.shortcuts import render

# Create your views here.
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated ,AllowAny
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from django.conf import settings
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from .models import User
from .serializers import (
    RegisterSerializer,
    LoginSerializer,
    GoogleAuthSerializer,
    UserSerializer,
    UpdateProfileSerializer,
    ChangePasswordSerializer,
)

def get_tokens(user):
    refresh =RefreshToken.for_user(user)
    return {
        'access':  str(refresh.access_token),
        'refresh': str(refresh),
    }

class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self,request):
        serializer = RegisterSerializer(data=request.body)
        if serializer.is_valid():
            user = serializer.save()
            tokens = get_token(user)
            return Response(
                {
                    'message':'Account Created Successful',
                    'user':UserSerializer(user).data,
                    **tokens
                }, status =status.HTTP_201_CREATED
            )
        return Response({
            'error':serializer.errors,
        },status =status.HTTP_400_BAD_REQUEST)