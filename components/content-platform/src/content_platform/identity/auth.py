"""Password-backed account registration and hashed bearer sessions."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import hmac
import secrets
import sqlite3
import time

from ..errors import AlreadyExists, AuthenticationFailed, ValidationError
from ..storage.database import Database
from ..timeutil import utc_timestamp


class AuthRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def register(
        self,
        *,
        user_id: str,
        display_name: str,
        password_salt: str,
        password_hash: str,
        created_at: str,
        now_epoch: float,
        token_hash: str,
        expires_at: float,
    ) -> None:
        try:
            with self.database.transaction() as connection:
                connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now_epoch,))
                connection.execute(
                    "INSERT INTO users(user_id, display_name, created_at) VALUES (?, ?, ?)",
                    (user_id, display_name, created_at),
                )
                connection.execute(
                    """INSERT INTO auth_credentials(user_id, password_salt, password_hash, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, password_salt, password_hash, created_at),
                )
                connection.execute(
                    """INSERT INTO auth_sessions(token_hash, user_id, created_at, expires_at)
                       VALUES (?, ?, ?, ?)""",
                    (token_hash, user_id, created_at, expires_at),
                )
        except Exception as exc:
            if self.database.is_user_id_conflict(exc):
                raise AlreadyExists("user already exists") from exc
            raise

    def credentials(self, user_id: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT password_salt, password_hash FROM auth_credentials WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    def create_session(
        self,
        user_id: str,
        token_hash: str,
        created_at: str,
        now_epoch: float,
        expires_at: float,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now_epoch,))
            connection.execute(
                """INSERT INTO auth_sessions(token_hash, user_id, created_at, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (token_hash, user_id, created_at, expires_at),
            )

    def session(self, token_hash: str) -> sqlite3.Row | None:
        with self.database.connect() as connection:
            return connection.execute(
                "SELECT token_hash, user_id, expires_at FROM auth_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()

    def delete_session(self, token_hash: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            )
            return cursor.rowcount == 1


class AuthService:
    """Authenticate users without persisting passwords or raw session tokens."""

    PBKDF2_ITERATIONS = 150_000
    _DUMMY_SALT = bytes.fromhex("7f" * 16)

    def __init__(
        self,
        repository: AuthRepository,
        *,
        session_ttl_seconds: int = 86_400,
        clock: Callable[[], str] = utc_timestamp,
        epoch_clock: Callable[[], float] = time.time,
    ) -> None:
        if session_ttl_seconds < 60:
            raise ValueError("session_ttl_seconds must be at least 60")
        self.repository = repository
        self.session_ttl_seconds = session_ttl_seconds
        self.clock = clock
        self.epoch_clock = epoch_clock
        self._dummy_hash = hashlib.pbkdf2_hmac(
            "sha256", b"invalid-account-password", self._DUMMY_SALT,
            self.PBKDF2_ITERATIONS,
        )

    @staticmethod
    def _user_fields(user_id: object, display_name: object) -> tuple[str, str]:
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id.strip()) > 100:
            raise ValidationError("user_id must contain 1 to 100 characters")
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name.strip()) > 200:
            raise ValidationError("display_name must contain 1 to 200 characters")
        return user_id.strip(), display_name.strip()

    @staticmethod
    def _password(value: object) -> str:
        if not isinstance(value, str) or not 12 <= len(value) <= 256:
            raise ValidationError("password must contain 12 to 256 characters")
        return value

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def _issue_session(self, user_id: str) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        now = self.epoch_clock()
        expires_at = now + self.session_ttl_seconds
        self.repository.create_session(
            user_id, self._token_hash(token), self.clock(), now, expires_at
        )
        return token, expires_at

    def register(self, user_id: object, display_name: object, password: object) -> dict[str, object]:
        normalized_id, normalized_name = self._user_fields(user_id, display_name)
        password_text = self._password(password)
        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac(
            "sha256", password_text.encode("utf-8"), salt, self.PBKDF2_ITERATIONS
        ).hex()
        token = secrets.token_urlsafe(32)
        now_text = self.clock()
        now_epoch = self.epoch_clock()
        expires_at = now_epoch + self.session_ttl_seconds
        self.repository.register(
            user_id=normalized_id,
            display_name=normalized_name,
            password_salt=salt.hex(),
            password_hash=password_hash,
            created_at=now_text,
            now_epoch=now_epoch,
            token_hash=self._token_hash(token),
            expires_at=expires_at,
        )
        return self._session_result(normalized_id, token, expires_at)

    def login(self, user_id: object, password: object) -> dict[str, object]:
        if not isinstance(user_id, str) or not user_id.strip() or len(user_id.strip()) > 100:
            raise ValidationError("user_id must contain 1 to 100 characters")
        password_text = self._password(password)
        normalized_id = user_id.strip()
        credential = self.repository.credentials(normalized_id)
        salt = bytes.fromhex(credential["password_salt"]) if credential else self._DUMMY_SALT
        expected = bytes.fromhex(credential["password_hash"]) if credential else self._dummy_hash
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password_text.encode("utf-8"), salt, self.PBKDF2_ITERATIONS
        )
        valid = hmac.compare_digest(candidate, expected)
        if credential is None or not valid:
            raise AuthenticationFailed("credentials were not accepted")
        token, expires_at = self._issue_session(normalized_id)
        return self._session_result(normalized_id, token, expires_at)

    def authenticate(self, token: object) -> str:
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii():
            raise AuthenticationFailed("bearer session was not accepted")
        candidate = self._token_hash(token)
        session = self.repository.session(candidate)
        expected = session["token_hash"] if session else "0" * 64
        matched = hmac.compare_digest(expected, candidate)
        if session is None or not matched or float(session["expires_at"]) <= self.epoch_clock():
            raise AuthenticationFailed("bearer session was not accepted")
        return str(session["user_id"])

    def revoke(self, token: object) -> bool:
        if not isinstance(token, str) or not 32 <= len(token) <= 256 or not token.isascii():
            raise AuthenticationFailed("bearer session was not accepted")
        candidate = self._token_hash(token)
        session = self.repository.session(candidate)
        expected = session["token_hash"] if session else "0" * 64
        if session is None or not hmac.compare_digest(expected, candidate):
            raise AuthenticationFailed("bearer session was not accepted")
        return self.repository.delete_session(candidate)

    @staticmethod
    def _session_result(user_id: str, token: str, expires_at: float) -> dict[str, object]:
        return {
            "user_id": user_id,
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": expires_at,
        }
