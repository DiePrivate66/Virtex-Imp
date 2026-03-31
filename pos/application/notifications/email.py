"""Application facade for transactional email delivery."""

from pos.infrastructure.notifications import ResendEmailError, send_resend_email

__all__ = ['ResendEmailError', 'send_resend_email']
