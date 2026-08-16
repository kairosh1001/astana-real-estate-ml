from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


PASSWORD_MAX_LENGTH = 128
PASSWORD_MIN_LENGTH = 10
DISPLAY_NAME_MAX_LENGTH = 60
EMAIL_MAX_LENGTH = 254
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PASSWORD_HASHER = PasswordHasher(type=Type.ID)


class AuthValidationError(ValueError):
    pass


def normalize_email(value: str) -> str:
    email = unicodedata.normalize("NFKC", value).strip().casefold()
    if not email or len(email) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.fullmatch(email):
        raise AuthValidationError("Введите корректный email.")
    local, domain = email.rsplit("@", 1)
    try:
        normalized_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise AuthValidationError("Введите корректный email.") from exc
    normalized = f"{local}@{normalized_domain}"
    if len(normalized) > EMAIL_MAX_LENGTH:
        raise AuthValidationError("Введите корректный email.")
    return normalized


def normalize_display_name(value: str) -> str:
    display_name = " ".join(unicodedata.normalize("NFKC", value).split())
    if len(display_name) < 2:
        raise AuthValidationError("Имя должно содержать хотя бы 2 символа.")
    if len(display_name) > DISPLAY_NAME_MAX_LENGTH:
        raise AuthValidationError(
            f"Имя должно быть не длиннее {DISPLAY_NAME_MAX_LENGTH} символов."
        )
    if any(ord(character) < 32 for character in display_name):
        raise AuthValidationError("Имя содержит недопустимые символы.")
    return display_name


def validate_password(value: str) -> str:
    if len(value) < PASSWORD_MIN_LENGTH:
        raise AuthValidationError(
            f"Пароль должен содержать хотя бы {PASSWORD_MIN_LENGTH} символов."
        )
    if len(value) > PASSWORD_MAX_LENGTH:
        raise AuthValidationError(
            f"Пароль должен быть не длиннее {PASSWORD_MAX_LENGTH} символов."
        )
    if value.isspace():
        raise AuthValidationError("Пароль не может состоять только из пробелов.")
    return value


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    if len(password) > PASSWORD_MAX_LENGTH:
        return False
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    try:
        return PASSWORD_HASHER.check_needs_rehash(encoded)
    except InvalidHashError:
        return True


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
