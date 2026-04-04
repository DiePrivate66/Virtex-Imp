web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && python scripts/start_web.py
worker: celery -A config worker -l info --concurrency 1
beat: celery -A config beat -l info
