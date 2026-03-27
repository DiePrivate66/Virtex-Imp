from django.urls import path

from .views import (
    api_integrations_health,
    api_print_job_ack,
    api_print_job_fail,
    api_print_job_retry,
    api_print_jobs_failed,
    api_print_jobs_pending,
    confirmar_venta_whatsapp,
    whatsapp_webhook,
)

urlpatterns = [
    path('integrations/whatsapp/webhook/', whatsapp_webhook, name='whatsapp_webhook'),
    path('api/ventas/<int:venta_id>/confirmar-whatsapp/', confirmar_venta_whatsapp, name='confirmar_venta_whatsapp'),
    path('api/print-jobs/pending/', api_print_jobs_pending, name='api_print_jobs_pending'),
    path('api/print-jobs/failed/', api_print_jobs_failed, name='api_print_jobs_failed'),
    path('api/print-jobs/<int:job_id>/ack/', api_print_job_ack, name='api_print_job_ack'),
    path('api/print-jobs/<int:job_id>/fail/', api_print_job_fail, name='api_print_job_fail'),
    path('api/print-jobs/<int:job_id>/retry/', api_print_job_retry, name='api_print_job_retry'),
    path('api/integrations/health/', api_integrations_health, name='api_integrations_health'),
]
