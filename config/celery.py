import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
app.conf.beat_schedule = {
    'sweep-delivery-quote-timeouts-every-minute': {
        'task': 'pos.infrastructure.tasks.sweep_delivery_quote_timeouts',
        'schedule': 60.0,
    },
    'requeue-stuck-print-jobs-every-minute': {
        'task': 'pos.infrastructure.tasks.requeue_stuck_print_jobs',
        'schedule': 60.0,
    },
}


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
