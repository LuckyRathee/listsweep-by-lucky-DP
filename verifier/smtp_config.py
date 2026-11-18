"""Validate SMTP identity without guessing a real external domain."""
import os
from email_validator import validate_email, EmailNotValidError
from dataclasses import dataclass


@dataclass(frozen=True)
class SMTPSettings:
    helo_domain: str = 'localhost'
    mail_from: str = 'probe@localhost'

    @classmethod
    def from_environment(cls):
        return cls(helo_domain=os.getenv('VERIFIER_HELO_DOMAIN', 'localhost'),
                   mail_from=os.getenv('VERIFIER_MAIL_FROM', 'probe@localhost'))

    @property
    def reason(self):
        if self.helo_domain == 'localhost' or self.mail_from == 'probe@localhost':
            return 'smtp_sender_identity_not_configured'
        try:
            valid = validate_email(self.mail_from, check_deliverability=False, test_environment=True)
        except EmailNotValidError:
            return 'invalid_smtp_sender_identity'
        if valid.domain.lower() != self.helo_domain.lower():
            return 'invalid_smtp_sender_identity'
        if any(not label or label.startswith('-') or label.endswith('-')
               or not label.isascii() or not all(c.isalnum() or c == '-' for c in label)
               for label in self.helo_domain.split('.')):
            return 'invalid_smtp_sender_identity'
        return None

    @property
    def ready(self):
        return self.reason is None
