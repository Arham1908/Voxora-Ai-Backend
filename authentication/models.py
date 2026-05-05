from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email Required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save()
        return user
        
    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, password, **extra_fields)

class User(AbstractBaseUser, PermissionsMixin):
    email =models.EmailField(unique=True)
    full_name =models.CharField(max_length=100,blank=True)
    avatar =models.URLField(blank=True)

    AUTH_PROVIDER_CHOICES =[
        ('email','Email'),
        ('google','Google')
    ]

    ROLE_CHOICES = [
        ('user','User'),
        ('admin','Admin')
    ]

    auth_provider = models.CharField(max_length=20,choices=AUTH_PROVIDER_CHOICES, default='email')
    is_active = models.BooleanField(default=True)
    role =models.CharField(max_length=20,choices=ROLE_CHOICES, default='user')

    created_at = models.DateTimeField(auto_now_add=True)
    USERNAME_FIELD  = 'email'
    REQUIRED_FIELDS = []

    objects =UserManager()

    def __str__(self):
        return self.email