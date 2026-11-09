"""The only component allowed to resolve and store a provider key."""
from __future__ import annotations

from re import escape, sub
from threading import RLock
from typing import Any

from p_mc_optimizer.application.errors import ApiError

_OS_BACKENDS = {
    "keyring.backends.macOS.Keyring",
    "keyring.backends.Windows.WinVaultKeyring",
    "keyring.backends.SecretService.Keyring",
    "keyring.backends.kwallet.DBusKeyring",
    "keyring.backends.libsecret.Keyring",
}
_SERVICE_PREFIX = "P_MC_Optimizer"


def _keyring_backend() -> Any:
    try:
        import keyring

        backend = keyring.get_keyring()
        name = f"{type(backend).__module__}.{type(backend).__name__}"
        return backend if name in _OS_BACKENDS else None
    except Exception:
        # The optional OS service may be absent or locked.
        return None


def _keyring_error() -> ApiError:
    return ApiError(
        422, "SECURE_KEYRING_UNAVAILABLE",
        "مخزن امن کلید سیستم‌عامل در دسترس نیست. حالت حافظهٔ نشست را انتخاب کنید.",
    )


class CredentialBroker:
    """Memory keys belong to this API process and are bound to an exact origin."""

    def __init__(self, *, internal_token: str | None = None):
        self._session: dict[tuple[str, str], str] = {}
        self._known_keys: dict[tuple[str, str], str] = {}
        self._keyring = _keyring_backend()
        self._token = internal_token
        self._lock = RLock()

    @property
    def os_keyring_available(self) -> bool:
        return self._keyring is not None

    @staticmethod
    def _service(origin: str) -> str:
        return f"{_SERVICE_PREFIX}:{origin}"

    def get(self, ref: str, origin: str, mode: str | None) -> str | None:
        with self._lock:
            if mode == "session":
                return self._session.get((ref, origin))
            if mode == "os_keyring" and self._keyring is not None:
                try:
                    key = self._keyring.get_password(self._service(origin), ref)
                except Exception:
                    return None
                if key:
                    self._known_keys[(ref, origin)] = key
                return key
            return None

    def _write_keyring(self, ref: str, origin: str, key: str | None) -> None:
        if self._keyring is None:
            raise _keyring_error()
        service = self._service(origin)
        try:
            if key is None:
                if self._keyring.get_password(service, ref) is not None:
                    self._keyring.delete_password(service, ref)
            else:
                self._keyring.set_password(service, ref, key)
            if self._keyring.get_password(service, ref) != key:
                raise RuntimeError("Keyring state could not be verified")
        except Exception:
            raise _keyring_error() from None

    def replace(
        self, ref: str, origin: str, old_mode: str | None, new_mode: str | None, key: str | None,
    ) -> None:
        """Update OS storage first, then in-memory state."""
        with self._lock:
            identity = (ref, origin)
            if new_mode == "os_keyring":
                self._write_keyring(ref, origin, key)
            elif old_mode == "os_keyring":
                self._write_keyring(ref, origin, None)
            if new_mode == "session" and key is not None:
                self._session[identity] = key
            else:
                self._session.pop(identity, None)
            if key is None:
                self._known_keys.pop(identity, None)
            else:
                self._known_keys[identity] = key

    def is_internal_token(self, value: str) -> bool:
        return value == self._token

    def sanitize(self, value: Any, *, extra_key: str | None = None) -> Any:
        with self._lock:
            secrets = {*self._known_keys.values(), self._token, extra_key} - {None, ""}
        ordered = sorted(secrets, key=len, reverse=True)

        def redact(item: Any) -> Any:
            if isinstance(item, str):
                for secret in ordered:
                    if len(secret) < 8:
                        item = sub(r"(?<!\w)" + escape(secret) + r"(?!\w)", "[REDACTED]", item)
                    else:
                        item = item.replace(secret, "[REDACTED]")
                return item
            if isinstance(item, dict):
                return {redact(key): redact(value) for key, value in item.items()}
            if isinstance(item, list):
                return [redact(value) for value in item]
            return item

        return redact(value)
