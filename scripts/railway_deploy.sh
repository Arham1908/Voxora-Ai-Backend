#!/bin/bash

# Ensure SQLite directory exists in volume mount
mkdir -p /app/data

# 1. Run migrations safely
echo "Applying database migrations..."
python manage.py migrate --noinput

# 2. Collective static files for Whitenoise
echo "Collecting static files..."
python manage.py collectstatic --noinput --clear

# 3. Create superuser from environment variables if set
# Expected variables: DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_EMAIL, DJANGO_SUPERUSER_PASSWORD
if [ -n "$DJANGO_SUPERUSER_USERNAME" ] && [ -n "$DJANGO_SUPERUSER_PASSWORD" ]; then
    echo "Creating superuser '$DJANGO_SUPERUSER_USERNAME'..."
    python manage.py shell <<EOF
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(username='$DJANGO_SUPERUSER_USERNAME').exists():
    User.objects.create_superuser('$DJANGO_SUPERUSER_USERNAME', '$DJANGO_SUPERUSER_EMAIL', '$DJANGO_SUPERUSER_PASSWORD')
    print('Superuser created successfully.')
else:
    print('Superuser already exists.')
EOF
fi

# 4. Start Daphne using the Railway injected PORT environment variable
echo "Starting Daphne on port $PORT..."
exec daphne -b 0.0.0.0 -p ${PORT:-8000} kfc_api.asgi:application
