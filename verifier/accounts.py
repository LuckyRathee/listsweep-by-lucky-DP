"""Accounts: password hashing, session tokens, password rules and sign-in throttling."""
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import deque

from email_validator import EmailNotValidError, validate_email

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD, MAX_PASSWORD = 10, 200      # the cap stops absurdly long inputs from costing CPU
COMMON_PASSWORDS = {'password', 'password1', '1234567890', 'qwertyuiop', 'letmein123', 'welcome123',
                    'admin12345', 'iloveyou12', 'changeme123'}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def hash_password(password: str) -> str:
    """Salted scrypt hash. Only this string is ever stored, never the password."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f'scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}'


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split('$')
        if scheme != 'scrypt':
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode('utf-8'), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the email is unknown, so "no such account" costs the same time as a wrong password.
DUMMY_HASH = hash_password(secrets.token_hex(8))


def normalize_email(raw: str) -> str:
    try:
        return validate_email((raw or '').strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as error:
        raise ValueError('Enter a valid email address') from error


def password_problem(password: str, email: str = '') -> str | None:
    """Rules for passwords people choose. Returns what is wrong, or None."""
    if len(password) < MIN_PASSWORD:
        return f'Use at least {MIN_PASSWORD} characters'
    if len(password) > MAX_PASSWORD:
        return f'Use at most {MAX_PASSWORD} characters'
    if password.lower() in COMMON_PASSWORDS:
        return 'That password is too common'
    classes = sum([any(c.islower() for c in password), any(c.isupper() for c in password),
                   any(c.isdigit() for c in password), any(not c.isalnum() for c in password)])
    if classes < 3:
        return 'Mix at least three of: lowercase, uppercase, numbers and symbols'
    local = email.split('@')[0].lower()
    if len(local) >= 4 and local in password.lower():
        return "Don't use your email name inside the password"
    return None


def temporary_password() -> str:
    """A random password for a new or reset account; the person must replace it at first sign-in."""
    return f'{secrets.token_urlsafe(9)}-{secrets.randbelow(90) + 10}A'


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class LoginThrottle:
    """Locks sign-in after repeated failures, per account+address and per address."""

    def __init__(self, limit: int = 5, ip_limit: int = 30, window: float = 900.0):
        self.limit, self.ip_limit, self.window = limit, ip_limit, window
        self._failures: dict[str, deque] = {}

    def _recent(self, key: str) -> deque:
        now = time.monotonic()
        queue = self._failures.setdefault(key, deque())
        while queue and now - queue[0] > self.window:
            queue.popleft()
        return queue

    def blocked_for(self, user_key: str, ip_key: str) -> int:
        """Seconds until another attempt is allowed, or 0."""
        now = time.monotonic()
        waits = [0]
        for key, limit in ((user_key, self.limit), (ip_key, self.ip_limit)):
            queue = self._recent(key)
            if len(queue) >= limit:
                waits.append(int(self.window - (now - queue[0])) + 1)
        return max(waits)

    def record(self, user_key: str, ip_key: str) -> None:
        now = time.monotonic()
        for key in (user_key, ip_key):
            self._recent(key).append(now)

    def clear(self, user_key: str) -> None:
        self._failures.pop(user_key, None)
