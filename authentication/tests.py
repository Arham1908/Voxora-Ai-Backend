from types import SimpleNamespace

from django.test import TestCase

from .models import User
from .serializers import ChangePasswordSerializer, GoogleAuthSerializer, LoginSerializer


class LoginSerializerTests(TestCase):
    def test_valid_login_adds_user_to_validated_data(self):
        user = User.objects.create_user(
            email="user@example.com",
            password="old-password-123",
            full_name="Test User",
        )

        serializer = LoginSerializer(
            data={
                "email": "user@example.com",
                "password": "old-password-123",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["user"], user)

    def test_blocked_account_is_rejected(self):
        User.objects.create_user(
            email="blocked@example.com",
            password="old-password-123",
            is_active=False,
        )

        serializer = LoginSerializer(
            data={
                "email": "blocked@example.com",
                "password": "old-password-123",
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("Account is blocked", str(serializer.errors))


class GoogleAuthSerializerTests(TestCase):
    def test_accepts_id_token_without_model_fields(self):
        serializer = GoogleAuthSerializer(data={"id_token": "token-value"})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["id_token"], "token-value")


class ChangePasswordSerializerTests(TestCase):
    def test_validates_old_password_and_updates_password(self):
        user = User.objects.create_user(
            email="password@example.com",
            password="old-password-123",
        )
        request = SimpleNamespace(user=user)
        serializer = ChangePasswordSerializer(
            data={
                "old_password": "old-password-123",
                "new_password": "new-password-456",
            },
            context={"request": request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        user.refresh_from_db()
        self.assertTrue(user.check_password("new-password-456"))

    def test_rejects_wrong_old_password(self):
        user = User.objects.create_user(
            email="wrong-old@example.com",
            password="old-password-123",
        )
        request = SimpleNamespace(user=user)
        serializer = ChangePasswordSerializer(
            data={
                "old_password": "not-the-old-password",
                "new_password": "new-password-456",
            },
            context={"request": request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("Old password is not correct", str(serializer.errors))

    def test_rejects_reusing_same_password(self):
        user = User.objects.create_user(
            email="same-password@example.com",
            password="old-password-123",
        )
        request = SimpleNamespace(user=user)
        serializer = ChangePasswordSerializer(
            data={
                "old_password": "old-password-123",
                "new_password": "old-password-123",
            },
            context={"request": request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("New password must be different", str(serializer.errors))
