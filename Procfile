release: python manage.py migrate --noinput && python manage.py collectstatic --noinput
web: gunicorn config.wsgi --workers 2 --timeout 120 --bind 0.0.0.0:$PORT
worker: celery -A config worker -l info --concurrency 2
beat: celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
