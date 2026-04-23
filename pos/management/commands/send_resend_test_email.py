from __future__ import annotations

from email.utils import parseaddr

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email

from pos.application.notifications import ResendEmailError, send_resend_email


class Command(BaseCommand):
    help = 'Envia un correo de prueba usando la configuracion Resend cargada por Django.'

    def add_arguments(self, parser):
        parser.add_argument('recipient_email', help='Correo destino para la prueba.')

    def handle(self, *args, **options):
        recipient_email = str(options['recipient_email'] or '').strip()
        try:
            validate_email(recipient_email)
        except ValidationError as exc:
            raise CommandError('recipient_email invalido') from exc

        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '')
        _, parsed_from = parseaddr(from_email)
        if not parsed_from:
            raise CommandError('DEFAULT_FROM_EMAIL invalido')

        api_key = getattr(settings, 'RESEND_API_KEY', '')
        if not api_key:
            raise CommandError('RESEND_API_KEY no esta configurado en este entorno')

        self.stdout.write('== RESEND TEST EMAIL ==')
        self.stdout.write(f'API base: {getattr(settings, "RESEND_API_BASE", "")}')
        self.stdout.write(f'From: {from_email}')
        self.stdout.write(f'To: {recipient_email}')
        self.stdout.write(f'API key: {_mask_secret(api_key)}')

        try:
            sent = send_resend_email(
                subject='Prueba Resend - RAMON by Bosco',
                text_body='Si recibes este correo, Django y Resend estan conectados correctamente.',
                html_body=(
                    '<p>Si recibes este correo, Django y Resend estan conectados correctamente.</p>'
                    '<p>Esta es una prueba operativa enviada desde el backend.</p>'
                ),
                recipient_email=recipient_email,
                from_email=from_email,
            )
        except ResendEmailError as exc:
            raise CommandError(f'Resend rechazo la prueba: {exc}') from exc

        if not sent:
            raise CommandError('Resend no devolvio id de mensaje')

        self.stdout.write(self.style.SUCCESS('OK: Resend acepto el correo de prueba.'))


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return '***'
    return f'{value[:4]}...{value[-4:]}'
