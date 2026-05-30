from __future__ import annotations

import json
import time
from http.cookiejar import CookieJar
from pathlib import Path
import requests


def save_requests_cookies(path: Path, cookies: requests.cookies.RequestsCookieJar) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = []
    now = time.time()
    for cookie in cookies:
        if cookie.expires is not None and cookie.expires <= now:
            continue
        data.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
            }
        )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def load_requests_cookies(path: Path) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    if not path.exists():
        return jar
    now = time.time()
    for item in json.loads(path.read_text()):
        expires = item.get("expires")
        if expires is not None and expires <= now:
            continue
        jar.set(
            item["name"],
            item["value"],
            domain=item.get("domain"),
            path=item.get("path") or "/",
            secure=bool(item.get("secure")),
            expires=item.get("expires"),
        )
    return jar


def cookie_header_to_jar(cookie_header: str, domain: str) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    for part in cookie_header.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name:
            jar.set(name, value, domain=domain, path="/")
    return jar


def has_any_cookie(jar: CookieJar) -> bool:
    now = time.time()
    return any(cookie.expires is None or cookie.expires > now for cookie in jar)
