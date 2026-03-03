web: gunicorn config.wsgi
worker: celery -A config worker -l info
beat: celery -A config beat -l info
