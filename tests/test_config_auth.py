from pathlib import Path
import time

import requests

from overleaf_sjtu.auth import cookie_header_to_jar, has_any_cookie, load_requests_cookies, save_requests_cookies
from overleaf_sjtu.config import Config, ConfigStore
from overleaf_sjtu import credentials
from overleaf_sjtu.credentials import PASSWORD_KEY_PREFIX, SERVICE_NAME, USERNAME_KEY


def test_config_roundtrip(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / "config.json")
    store.save(Config(current_project="0123456789abcdefabcdefab"))
    assert store.load().current_project == "0123456789abcdefabcdefab"


def test_cookie_header_to_jar() -> None:
    jar = cookie_header_to_jar("a=1; b=two", "latex.sjtu.edu.cn")
    assert jar.get("a") == "1"
    assert jar.get("b") == "two"


def test_cookie_persistence_skips_expired_entries(tmp_path: Path) -> None:
    jar = requests.cookies.RequestsCookieJar()
    jar.set("expired", "x", domain="latex.sjtu.edu.cn", path="/", expires=int(time.time()) - 1)
    jar.set("live", "y", domain="latex.sjtu.edu.cn", path="/", expires=int(time.time()) + 3600)
    path = tmp_path / "cookies.json"

    save_requests_cookies(path, jar)
    loaded = load_requests_cookies(path)

    assert loaded.get("expired") is None
    assert loaded.get("live") == "y"
    assert has_any_cookie(loaded)


def test_credentials_share_canvas_keyring_names() -> None:
    assert SERVICE_NAME == "canvas"
    assert USERNAME_KEY == "jaccount.username"
    assert PASSWORD_KEY_PREFIX == "jaccount.password:"


def test_delete_saved_credentials_removes_current_username_and_password(monkeypatch) -> None:
    store = {
        ("canvas", "jaccount.username"): "hammer",
        ("canvas", "jaccount.password:hammer"): "secret",
        ("canvas-cli", "jaccount.username"): "hammer",
        ("canvas-cli", "jaccount.password:hammer"): "legacy-secret",
    }

    class FakeKeyring:
        @staticmethod
        def get_password(service, key):
            return store.get((service, key))

        @staticmethod
        def delete_password(service, key):
            if (service, key) not in store:
                raise RuntimeError("missing")
            del store[(service, key)]

    monkeypatch.setattr(credentials, "keyring", FakeKeyring)

    deleted = credentials.delete_saved_credentials()

    assert "canvas:jaccount.username" in deleted
    assert "canvas:jaccount.password:hammer" in deleted
    assert ("canvas", "jaccount.username") not in store
    assert ("canvas", "jaccount.password:hammer") not in store
