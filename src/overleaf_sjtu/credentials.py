from __future__ import annotations

from typing import Optional

import keyring

SERVICE_NAME = "canvas"
LEGACY_SERVICE_NAMES = ("canvas-cli", "sjtu-canvas-cli")
USERNAME_KEY = "jaccount.username"
PASSWORD_KEY_PREFIX = "jaccount.password:"


def _password_key(username: str) -> str:
    return f"{PASSWORD_KEY_PREFIX}{username}"


def _service_names() -> tuple[str, ...]:
    return (SERVICE_NAME, *LEGACY_SERVICE_NAMES)


def _get_password(key: str) -> Optional[str]:
    for service_name in _service_names():
        try:
            value = keyring.get_password(service_name, key)
        except Exception:
            continue
        if value:
            return value
    return None


def get_saved_username() -> Optional[str]:
    username = _get_password(USERNAME_KEY)
    if not username:
        return None
    username = username.strip()
    return username or None


def get_saved_password(username: str) -> Optional[str]:
    if not username:
        return None
    return _get_password(_password_key(username))


def get_saved_credentials() -> tuple[Optional[str], Optional[str]]:
    username = get_saved_username()
    if not username:
        return None, None
    return username, get_saved_password(username)


def save_credentials(username: str, password: str) -> None:
    if not username:
        raise ValueError("username is empty")
    keyring.set_password(SERVICE_NAME, USERNAME_KEY, username)
    keyring.set_password(SERVICE_NAME, _password_key(username), password)


def delete_saved_credentials(username: str | None = None) -> list[str]:
    saved_username = get_saved_username()
    usernames = []
    for item in (username, saved_username):
        if item and item not in usernames:
            usernames.append(item)

    deleted: list[str] = []
    for service_name in _service_names():
        if not username or username == saved_username:
            if _delete_password(service_name, USERNAME_KEY):
                deleted.append(f"{service_name}:{USERNAME_KEY}")
        for item in usernames:
            key = _password_key(item)
            if _delete_password(service_name, key):
                deleted.append(f"{service_name}:{key}")
    return deleted


def _delete_password(service_name: str, key: str) -> bool:
    try:
        keyring.delete_password(service_name, key)
        return True
    except Exception:
        return False
