web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
worker: celery -A config worker -l info --concurrency 1
beat: celery -A config beat -l info
