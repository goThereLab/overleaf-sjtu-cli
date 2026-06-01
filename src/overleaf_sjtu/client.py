from __future__ import annotations

import json
import mimetypes
import posixpath
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .auth import has_any_cookie, load_requests_cookies, save_requests_cookies
from .config import Config, ConfigStore
from .models import CompileResult, FileEntry, Project
from .parsers import extract_csrf, extract_jaccount_login_context, page_requires_captcha, parse_project_id, parse_project_list

JACCOUNT_BASE = "https://jaccount.sjtu.edu.cn"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class OverleafError(RuntimeError):
    pass


class AuthRequired(OverleafError):
    pass


class JAccountVerificationRequired(AuthRequired):
    def __init__(self, challenge: "JAccountVerificationChallenge", response: requests.Response) -> None:
        self.challenge = challenge
        self.response = response
        methods = ", ".join(challenge.methods) or "unknown"
        super().__init__(f"jAccount requires additional verification: {methods}")


@dataclass
class JAccountVerificationChallenge:
    url: str
    account: str | None
    methods: list[str]


@dataclass
class JAccountVerificationResult:
    success: bool
    message: str
    retry_seconds: int | None = None


class SocketIO09Client:
    def __init__(self, client: "OverleafClient", project_id: str) -> None:
        self.client = client
        self.project_id = project_id
        self.session = client.session
        self.sid: str | None = None
        self.poll_url: str | None = None
        self.ack_id = 0

    def connect(self) -> None:
        self.client._ensure_authenticated_locally()
        resp = self.session.get(
            self.client._url("/socket.io/1/"),
            params={"projectId": self.project_id, "t": int(time.time() * 1000)},
            timeout=self.client.timeout,
        )
        self.client._raise_for_auth(resp)
        if resp.status_code >= 400:
            raise OverleafError(self.client._error_text(resp))
        self.sid = resp.text.split(":", 1)[0]
        self.poll_url = self.client._url(f"/socket.io/1/xhr-polling/{self.sid}")

    def poll(self, timeout: int | None = None) -> list[str]:
        if not self.poll_url:
            raise OverleafError("socket is not connected")
        self.client._ensure_authenticated_locally()
        resp = self.session.get(
            self.poll_url,
            params={"projectId": self.project_id, "t": int(time.time() * 1000)},
            timeout=timeout or max(self.client.timeout, 65),
        )
        self.client._raise_for_auth(resp)
        if resp.status_code >= 400:
            raise OverleafError(self.client._error_text(resp))
        return self._packets(resp.content.decode("utf-8", "replace"))

    def emit_ack(self, name: str, args: list, timeout: int | None = None) -> list | None:
        self.ack_id += 1
        ack = self.ack_id
        packet = "5:%d+::%s" % (
            ack,
            json.dumps({"name": name, "args": args}, ensure_ascii=False, separators=(",", ":")),
        )
        self._send(packet)
        deadline = time.time() + (timeout or max(self.client.timeout, 65))
        while time.time() < deadline:
            for item in self.poll(timeout=max(5, min(15, int(deadline - time.time()) or 5))):
                if item.startswith(f"6:::{ack}+"):
                    return json.loads(item[len(f"6:::{ack}+") :])
                if item.startswith(f"6:::{ack}"):
                    return None
                if item.startswith("7:::"):
                    raise OverleafError(f"socket error: {item}")
        raise OverleafError(f"socket ack timed out for {name}")

    def wait_event(self, name: str, timeout: int | None = None) -> dict:
        deadline = time.time() + (timeout or max(self.client.timeout, 65))
        while time.time() < deadline:
            for item in self.poll(timeout=max(5, min(15, int(deadline - time.time()) or 5))):
                if not item.startswith("5:::"):
                    continue
                data = json.loads(item[4:])
                if data.get("name") == name:
                    return data
        raise OverleafError(f"socket event timed out: {name}")

    def _send(self, packet: str) -> None:
        if not self.poll_url:
            raise OverleafError("socket is not connected")
        self.client._ensure_authenticated_locally()
        resp = self.session.post(
            self.poll_url,
            params={"projectId": self.project_id, "t": int(time.time() * 1000)},
            data=packet.encode("utf-8"),
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            timeout=self.client.timeout,
        )
        self.client._raise_for_auth(resp)
        if resp.status_code >= 400:
            raise OverleafError(self.client._error_text(resp))

    def _packets(self, payload: str) -> list[str]:
        if not payload:
            return []
        if payload[0] != "\ufffd":
            return [payload]
        packets = []
        index = 0
        while index < len(payload):
            end = payload.index("\ufffd", index + 1)
            size = int(payload[index + 1 : end])
            start = end + 1
            packets.append(payload[start : start + size])
            index = start + size
        return packets


class OverleafClient:
    def __init__(self, config: Config, store: ConfigStore, timeout: int = 60) -> None:
        self.config = config
        self.store = store
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": _UA,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            }
        )
        self.session.cookies.update(load_requests_cookies(store.cookie_path))

    @property
    def base_url(self) -> str:
        return self.config.base_url.rstrip("/")

    @property
    def project_url(self) -> str:
        return self.config.project_url

    def save_cookies(self) -> None:
        save_requests_cookies(self.store.cookie_path, self.session.cookies)

    def begin_jaccount_login(self) -> dict:
        resp = self.session.get(self._url("/jaccountlogin"), allow_redirects=False, timeout=self.timeout)
        resp.raise_for_status()
        while resp.is_redirect:
            location = resp.headers.get("Location")
            if not location:
                break
            resp = self.session.get(urljoin(resp.url, location), allow_redirects=False, timeout=self.timeout)
            resp.raise_for_status()
        html = resp.text
        context = extract_jaccount_login_context(html)
        if not context:
            raise OverleafError("could not find jAccount login context from redirected login page")
        captcha_url = urljoin(JACCOUNT_BASE, "/jaccount/captcha")
        return {
            "login_url": resp.url,
            "post_url": urljoin(resp.url, "ulogin"),
            "captcha_url": captcha_url,
            "requires_captcha": page_requires_captcha(html),
            "context": context,
        }

    def get_login_captcha(self, login_state: dict) -> bytes:
        context = login_state["context"]
        resp = self.session.get(
            login_state["captcha_url"],
            params={"uuid": context["uuid"], "t": int(time.time() * 1000)},
            headers={"Referer": login_state["login_url"]},
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        return resp.content

    def download_login_captcha(self, login_state: dict, output: Path) -> Path:
        content = self.get_login_captcha(login_state)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        return output

    def login_with_jaccount(
        self,
        username: str,
        password: str,
        captcha: str | None = None,
        login_state: dict | None = None,
        mfa_method: str | None = None,
        mfa_code: str | None = None,
        trust_mfa: bool = True,
    ) -> dict:
        state = login_state or self.begin_jaccount_login()
        context = state["context"]
        payload = {
            "sid": context.get("sid", ""),
            "client": context.get("client", ""),
            "returl": context.get("returl", ""),
            "se": context.get("se", ""),
            "v": context.get("v", ""),
            "uuid": context.get("uuid", ""),
            "user": username,
            "pass": password,
            "captcha": captcha or "",
            "lt": "p",
        }
        resp = self.session.post(
            state["post_url"],
            data=payload,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": state["login_url"],
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        data = self._json_or_empty(resp)
        if not _errno_is_success(data.get("errno")):
            message = data.get("error") or data.get("message") or "jAccount login failed"
            if "captcha" in str(message).lower():
                message += "; rerun `overleaf auth login` and enter the captcha from the saved image"
            raise AuthRequired(str(message))
        redirect_url = data.get("url")
        if not redirect_url:
            raise OverleafError(f"jAccount login succeeded without redirect URL: {data}")
        callback = self.session.get(urljoin(resp.url, redirect_url), allow_redirects=True, timeout=self.timeout)
        if callback.status_code >= 400:
            raise OverleafError(self._error_text(callback))
        challenge = self._jaccount_verification_challenge(callback)
        if challenge:
            if not mfa_method or not mfa_code:
                raise JAccountVerificationRequired(challenge, callback)
            return self.complete_jaccount_verification(callback, mfa_method, mfa_code, trust=trust_mfa, request_code=False)
        self.save_cookies()
        return self.whoami()

    def complete_jaccount_verification(
        self,
        challenge_resp: requests.Response,
        method: str,
        code: str,
        trust: bool = True,
        request_code: bool = True,
        account: str | None = None,
    ) -> dict:
        if request_code:
            sent = self.request_jaccount_verification(challenge_resp, method)
            if not sent.success:
                raise AuthRequired(sent.message)
        verified = self.submit_jaccount_verification(challenge_resp, code, trust=trust, account=account)
        if not verified.success:
            raise AuthRequired(verified.message)
        callback = self.session.get(challenge_resp.url, allow_redirects=True, timeout=self.timeout)
        if callback.status_code >= 400:
            raise OverleafError(self._error_text(callback))
        if self._jaccount_verification_challenge(callback):
            raise AuthRequired(
                "jAccount additional verification did not complete; "
                "check the code or app approval, then retry `overleaf auth flow mfa-submit --code CODE`"
            )
        self.save_cookies()
        return self.whoami()

    def _looks_like_jaccount_verification(self, resp: requests.Response) -> bool:
        return self._jaccount_verification_challenge(resp) is not None

    def _jaccount_verification_challenge(self, resp: requests.Response) -> JAccountVerificationChallenge | None:
        parsed = urlparse(resp.url)
        if "jaccount" not in parsed.netloc.lower():
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        methods = [
            str(item.get("value"))
            for item in soup.select('input[name="c"]')
            if item.get("value") in {"app", "email", "sms"}
        ]
        has_shouldauth = soup.find("input", attrs={"name": "shouldauth", "value": "true"}) is not None
        has_captcha = soup.find("input", attrs={"name": "captcha"}) is not None
        if not (has_shouldauth and has_captcha and methods):
            return None
        return JAccountVerificationChallenge(url=resp.url, account=self._jaccount_verification_account(soup, resp.text), methods=methods)

    def _jaccount_verification_account(self, soup: BeautifulSoup, html: str) -> str | None:
        account_input = soup.find("input", attrs={"name": "account"})
        if account_input and account_input.get("value"):
            return str(account_input.get("value"))
        for pattern in (
            r"\baccount\s*:\s*['\"]([^'\"]+)['\"]",
            r"\baccount\s*=\s*['\"]([^'\"]+)['\"]",
            r"['\"]account['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        ):
            account_match = re.search(pattern, html)
            if account_match:
                return account_match.group(1)
        return None

    def request_jaccount_verification(self, challenge_resp: requests.Response, method: str) -> JAccountVerificationResult:
        if method not in {"app", "email", "sms"}:
            raise ValueError("verification method must be app, email, or sms")
        resp = self.session.post(
            urljoin(challenge_resp.url, "2fa/loginVerify"),
            data={"c": method},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": challenge_resp.url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        data = self._json_or_empty(resp)
        entity = (data.get("entities") or [{}])[0] if isinstance(data.get("entities"), list) else {}
        message = str(
            entity.get("msg")
            or data.get("error")
            or data.get("message")
            or "verification code request did not return a success flag"
        )
        return JAccountVerificationResult(
            success=_verification_success(data, entity),
            message=message,
            retry_seconds=_int_or_none(entity.get("retrySeconds")),
        )

    def submit_jaccount_verification(
        self,
        challenge_resp: requests.Response,
        code: str,
        trust: bool = True,
        account: str | None = None,
    ) -> JAccountVerificationResult:
        challenge = self._jaccount_verification_challenge(challenge_resp)
        account = account or (challenge.account if challenge else None)
        if not account:
            raise OverleafError("could not find jAccount account for additional verification")
        normalized_code = _normalize_verification_code(code)
        if not normalized_code:
            raise AuthRequired("jAccount verification code is empty")
        resp = self.session.post(
            urljoin(challenge_resp.url, "2faVerify"),
            data={"account": account, "captcha": normalized_code, "trust": "true" if trust else "false"},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Referer": challenge_resp.url,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        data = self._json_or_empty(resp)
        return JAccountVerificationResult(
            success=_errno_is_success(data.get("errno")),
            message=str(data.get("error") or data.get("message") or "verification submit did not return a success flag"),
        )

    def get_csrf(self) -> str | None:
        resp = self._request("get", self.project_url, timeout=self.timeout)
        resp.raise_for_status()
        csrf = extract_csrf(resp.text)
        self.save_cookies()
        return csrf

    def whoami(self) -> dict:
        projects = self.list_projects()
        project_count = len(projects)
        cached_count = int(self.config.defaults.get("last_project_count_visible") or 0)
        if project_count == 0 and cached_count > 0:
            project_count = cached_count
        return {
            "base_url": self.base_url,
            "project_url": self.project_url,
            "authenticated": True,
            "project_count_visible": project_count,
        }

    def list_projects(self) -> list[Project]:
        projects = self._list_projects_once(cache_bust=False)
        cached_count = int(self.config.defaults.get("last_project_count_visible") or 0)
        if not projects and cached_count > 0:
            projects = self._list_projects_once(cache_bust=True)
        if projects:
            self.config.defaults["last_project_count_visible"] = len(projects)
            self.store.save(self.config)
        return projects

    def _list_projects_once(self, cache_bust: bool) -> list[Project]:
        url = self._cache_bust_url(self.project_url) if cache_bust else self.project_url
        kwargs = {"timeout": self.timeout}
        if cache_bust:
            kwargs["headers"] = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        resp = self._request("get", url, **kwargs)
        resp.raise_for_status()
        self.save_cookies()
        return parse_project_list(resp.text)

    def resolve_project(self, project: str | None = None) -> str:
        if project:
            return parse_project_id(project)
        if self.config.current_project:
            return self.config.current_project
        raise OverleafError("missing project id; run `overleaf project list` then `overleaf project select <id>`")

    def show_project(self, project: str) -> dict:
        pid = parse_project_id(project)
        resp = self._request("get", self._url(f"/project/{pid}"), timeout=self.timeout)
        resp.raise_for_status()
        self.save_cookies()
        return {
            "id": pid,
            "url": self._url(f"/project/{pid}"),
            "csrf": bool(extract_csrf(resp.text)),
            "html_bytes": len(resp.text),
        }

    def upload_project(self, archive: Path, name: str | None = None) -> Project:
        if not archive.exists():
            raise OverleafError(f"archive not found: {archive}")
        csrf = self.get_csrf()
        fields = {}
        if csrf:
            fields["_csrf"] = csrf
        if name:
            fields["projectName"] = name
            fields["name"] = name

        with archive.open("rb") as fh:
            files = {"qqfile": (archive.name, fh, "application/zip")}
            resp = self._request(
                "post",
                self._url("/project/new/upload"),
                data=fields,
                files=files,
                headers=self._csrf_headers(csrf),
                timeout=max(self.timeout, 180),
            )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        self.save_cookies()

        data = self._json_or_empty(resp)
        pid = self._project_id_from_response(resp, data)
        if not pid:
            raise OverleafError(f"upload succeeded but project id was not found: {data or resp.text[:200]}")
        return Project(id=pid, name=name or archive.stem, raw=data)

    def create_project(self, name: str) -> Project:
        name = name.strip()
        if not name:
            raise OverleafError("project name is required")
        csrf = self.get_csrf()
        attempts = [
            ("post", "/project/new", {"projectName": name, "name": name}, False),
            ("post", "/project/new", {"projectName": name}, True),
            ("post", "/project/new/blank", {"projectName": name, "name": name}, False),
        ]
        last: requests.Response | None = None
        for method, path, payload, json_request in attempts:
            fields = dict(payload)
            if csrf and not json_request:
                fields["_csrf"] = csrf
            resp = self._request(
                method,
                self._url(path),
                json=fields if json_request else None,
                data=None if json_request else fields,
                headers=self._csrf_headers(csrf, json_request=json_request),
                timeout=self.timeout,
            )
            last = resp
            if resp.status_code in {404, 405, 415}:
                continue
            if resp.status_code >= 400:
                raise OverleafError(self._error_text(resp))
            data = self._json_or_empty(resp)
            pid = self._project_id_from_response(resp, data)
            if pid:
                self.save_cookies()
                return Project(id=pid, name=name, raw=data)
        assert last is not None
        raise OverleafError(f"create project succeeded but project id was not found: {self._error_text(last)}")

    def download_project_zip(self, project: str, output: Path | None = None) -> Path:
        pid = parse_project_id(project)
        resp = self._request("get", self._url(f"/project/{pid}/download/zip"), stream=True, timeout=max(self.timeout, 180))
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        ctype = resp.headers.get("content-type", "")
        if "zip" not in ctype and not resp.headers.get("content-disposition"):
            raise OverleafError(f"download did not return a zip file: content-type={ctype or '-'}")

        output_path = output or Path(self._filename_from_disposition(resp.headers.get("content-disposition")) or f"{pid}.zip")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 128):
                if chunk:
                    fh.write(chunk)
        self.save_cookies()
        return output_path

    def delete_project(self, project: str) -> None:
        pid = parse_project_id(project)
        csrf = self.get_csrf()
        attempts = [
            ("delete", f"/project/{pid}", None),
            ("post", f"/project/{pid}/delete", {"_csrf": csrf} if csrf else {}),
            ("post", f"/project/{pid}/trash", {"_csrf": csrf} if csrf else {}),
        ]
        last: requests.Response | None = None
        for method, path, data in attempts:
            resp = self._request(
                method,
                self._url(path),
                data=data,
                headers=self._csrf_headers(csrf),
                timeout=self.timeout,
            )
            last = resp
            if resp.status_code in {200, 204, 302}:
                self.config.defaults.get("folder_ids", {}).pop(pid, None)
                self.config.defaults.get("project_outputs", {}).pop(pid, None)
                self.store.save(self.config)
                self.save_cookies()
                return
            if resp.status_code in {404, 405}:
                continue
        assert last is not None
        raise OverleafError(self._error_text(last))

    def list_entities(self, project: str) -> list[dict]:
        pid = parse_project_id(project)
        resp = self._request("get", self._url(f"/project/{pid}/entities"), timeout=self.timeout)
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        data = self._json_or_empty(resp)
        entities = data.get("entities") or data.get("data") or []
        if not isinstance(entities, list):
            raise OverleafError(f"unexpected entities response: {data}")
        self.save_cookies()
        return [item for item in entities if isinstance(item, dict) and item.get("path")]

    def list_project_path(self, project: str, remote_path: str = "/") -> list[FileEntry]:
        base = self._normalize_remote_path(remote_path, directory=True)
        prefix = "" if base == "/" else base.rstrip("/") + "/"
        by_name: dict[str, FileEntry] = {}
        for entity in self.list_entities(project):
            path = self._normalize_remote_path(str(entity["path"]))
            if path == base and base != "/":
                by_name[posixpath.basename(path)] = FileEntry(
                    path=path,
                    name=posixpath.basename(path),
                    type=str(entity.get("type") or "file"),
                    raw=entity,
                )
                continue
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix) :].strip("/")
            if not rest:
                continue
            head = rest.split("/", 1)[0]
            child_path = posixpath.join(base, head) if base != "/" else f"/{head}"
            if "/" in rest:
                by_name.setdefault(head, FileEntry(path=child_path, name=head, type="dir"))
            else:
                by_name[head] = FileEntry(
                    path=child_path,
                    name=head,
                    type=str(entity.get("type") or "file"),
                    raw=entity,
                )
        return sorted(by_name.values(), key=lambda item: (item.type != "dir", item.name.lower()))

    def download_project_path(self, project: str, remote_path: str, output: Path | None = None) -> Path:
        pid = parse_project_id(project)
        source = self._normalize_remote_path(remote_path, directory=False)
        with tempfile.TemporaryDirectory(prefix="overleaf-sjtu-") as tmp:
            archive = Path(tmp) / f"{pid}.zip"
            self.download_project_zip(pid, archive)
            with zipfile.ZipFile(archive) as zf:
                names = [name for name in zf.namelist() if name and not name.endswith("/")]
                wanted = source.strip("/")
                matched = [name for name in names if name == wanted or name.startswith(wanted.rstrip("/") + "/")]
                if source == "/":
                    matched = names
                if not matched:
                    raise OverleafError(f"remote path not found in project source: {source}")
                is_dir = source == "/" or any(name.startswith(wanted.rstrip("/") + "/") for name in matched)
                if is_dir:
                    out = output or Path(posixpath.basename(source.rstrip("/")) or pid)
                    out.mkdir(parents=True, exist_ok=True)
                    root_prefix = "" if source == "/" else wanted.rstrip("/") + "/"
                    for name in matched:
                        relative = name[len(root_prefix) :] if root_prefix else name
                        if not relative:
                            continue
                        target = self._safe_join(out, relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(name))
                    self.save_cookies()
                    return out
                out = output or Path(posixpath.basename(source))
                if out.exists() and out.is_dir():
                    out = out / posixpath.basename(source)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(zf.read(matched[0]))
                self.save_cookies()
                return out

    def create_folder(self, project: str, remote_path: str) -> dict:
        pid = parse_project_id(project)
        parts = self._remote_parts(remote_path)
        if not parts:
            raise OverleafError("cannot create project root")
        parent_id: str | None = None
        created: dict | None = None
        existing_paths = {self._normalize_remote_path(entity["path"]) for entity in self.list_entities(pid)}
        known_folders = self._folder_paths_from_entities(existing_paths)
        current = ""
        for name in parts:
            current = f"{current}/{name}"
            if current in known_folders:
                if parent_id is None:
                    raise OverleafError(
                        f"remote folder already exists but this Overleaf endpoint does not expose its id: {current}"
                    )
                raise OverleafError(f"remote folder already exists in this session: {current}")
            created = self._create_folder_under(pid, name, parent_id)
            parent_id = self._entity_id(created)
            if not parent_id:
                raise OverleafError(f"folder creation response did not include an id: {created}")
            known_folders.add(current)
            self._cache_folder_id(pid, current, parent_id)
        assert created is not None
        return created

    def upload_file_path(self, project: str, local_path: Path, remote_path: str | None = None) -> list[FileEntry]:
        if not local_path.exists():
            raise OverleafError(f"local path not found: {local_path}")
        pid = parse_project_id(project)
        remote = self._normalize_remote_path(remote_path or f"/{local_path.name}", directory=local_path.is_dir())
        if remote == "/":
            raise OverleafError("upload target cannot be project root; pass a file or directory path")
        entities = self.list_entities(pid)
        entity_paths = {self._normalize_remote_path(entity["path"]) for entity in entities}
        folder_paths = self._folder_paths_from_entities(entity_paths)
        if local_path.is_dir():
            editable = [path for path in sorted(local_path.rglob("*")) if path.is_file() and self._is_editable_doc_path(path.name)]
            if editable:
                sample = editable[0].relative_to(local_path).as_posix()
                raise OverleafError(
                    "file upload is pure HTTP and cannot write editable Overleaf docs "
                    f"({sample}); upload binary assets, or upload a project zip for LaTeX source changes"
                )
            if remote in folder_paths:
                raise OverleafError(f"remote folder already exists and cannot be addressed by id through /entities: {remote}")
            folder_id = self._create_folder_tree(pid, self._remote_parts(remote), folder_paths)
            return self._upload_directory_contents(pid, local_path, folder_id, remote)

        parent = posixpath.dirname(remote) or "/"
        filename = posixpath.basename(remote)
        if remote in entity_paths:
            if self._is_editable_doc_path(filename):
                return [self.upload_text_doc(pid, local_path, remote)]
            raise OverleafError(f"remote file already exists: {remote}")
        if self._is_editable_doc_path(filename):
            return [self.upload_text_doc(pid, local_path, remote)]
        if parent == "/":
            raise OverleafError("root binary upload needs the project root folder id, which /entities does not expose")
        cached_parent_id = self._cached_folder_id(pid, parent)
        if cached_parent_id:
            folder_id = cached_parent_id
        elif parent in folder_paths:
            folder_id = self._cached_folder_id(pid, parent)
            if not folder_id:
                raise OverleafError(f"remote folder already exists and cannot be addressed by id through /entities: {parent}")
        else:
            folder_id = self._create_folder_tree(pid, self._remote_parts(parent), folder_paths)
        uploaded = self._upload_one_file(pid, local_path, folder_id, filename)
        return [FileEntry(path=remote, name=filename, type=str(uploaded.get("entity_type") or "file"), raw=uploaded)]

    def upload_text_doc(self, project: str, local_path: Path, remote_path: str) -> FileEntry:
        pid = parse_project_id(project)
        remote = self._normalize_remote_path(remote_path)
        name = posixpath.basename(remote)
        parent = posixpath.dirname(remote) or "/"
        try:
            text = local_path.read_text()
        except UnicodeDecodeError as exc:
            raise OverleafError(f"text upload requires a decodable text file: {local_path}") from exc
        tree = self.get_project_file_tree(pid)
        folders = tree["folders"]
        entries = tree["entries"]
        existing = entries.get(remote)
        if existing and existing["type"] == "folder":
            raise OverleafError(f"remote path is a folder: {remote}")
        parent_id = self._ensure_folder_for_text_upload(pid, parent, folders)
        old_root_doc_id = tree.get("rootDoc_id")
        replacing_root_doc = bool(existing and existing["type"] == "doc" and existing["id"] == old_root_doc_id)
        if existing:
            self._delete_tree_entity(pid, existing["type"], existing["id"])
        doc = self._create_doc_under(pid, name, parent_id)
        doc_id = self._entity_id(doc)
        if not doc_id:
            raise OverleafError(f"doc creation response did not include an id: {doc}")
        self._write_doc_text(pid, doc_id, text)
        if replacing_root_doc:
            self._set_root_doc(pid, doc_id)
        self.save_cookies()
        return FileEntry(path=remote, name=name, type="doc", raw={"_id": doc_id})

    def get_project_file_tree(self, project: str) -> dict:
        pid = parse_project_id(project)
        socket = SocketIO09Client(self, pid)
        socket.connect()
        socket.poll()
        event = socket.wait_event("joinProjectResponse")
        project_data = event["args"][0]["project"]
        root = project_data["rootFolder"][0]
        folders: dict[str, str] = {"/": root["_id"]}
        entries: dict[str, dict[str, str]] = {}

        def walk(folder: dict, base: str) -> None:
            for item in folder.get("docs", []):
                path = self._normalize_remote_path(posixpath.join(base, item["name"]))
                entries[path] = {"id": item["_id"], "type": "doc", "parent_id": folder["_id"]}
            for item in folder.get("fileRefs", []):
                path = self._normalize_remote_path(posixpath.join(base, item["name"]))
                entries[path] = {"id": item["_id"], "type": "file", "parent_id": folder["_id"]}
            for child in folder.get("folders", []):
                path = self._normalize_remote_path(posixpath.join(base, child["name"]))
                folders[path] = child["_id"]
                entries[path] = {"id": child["_id"], "type": "folder", "parent_id": folder["_id"]}
                walk(child, path)

        walk(root, "/")
        return {"project": project_data, "rootDoc_id": project_data.get("rootDoc_id"), "folders": folders, "entries": entries}

    def compile(
        self,
        project: str,
        draft: bool = False,
        timeout_seconds: int = 120,
        stop_on_first: bool = False,
        compiler: str | None = None,
    ) -> CompileResult:
        pid = parse_project_id(project)
        csrf = self.get_csrf()
        payload = {"draft": draft, "check": "silent", "stopOnFirstError": stop_on_first}
        if compiler:
            payload["compiler"] = compiler
        resp = self._request(
            "post",
            self._url(f"/project/{pid}/compile"),
            json=payload,
            headers=self._csrf_headers(csrf, json_request=True),
            timeout=max(self.timeout, timeout_seconds + 15),
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        data = self._json_or_empty(resp)
        result = self._compile_result(pid, data)

        deadline = time.time() + timeout_seconds
        while result.status in {"compiling", "pending", "running"} and time.time() < deadline:
            time.sleep(1.5)
            status = self.compile_status(pid)
            if status:
                result = status
            else:
                break
        if result.pdf_url:
            self._save_output_url(pid, "pdf_url", result.pdf_url)
        if result.log_url:
            self._save_output_url(pid, "log_url", result.log_url)
        self.save_cookies()
        return result

    def compile_status(self, project: str) -> CompileResult | None:
        pid = parse_project_id(project)
        stored_outputs = self.config.defaults.get("project_outputs", {}).get(pid, {})
        for path in (f"/project/{pid}/compile/status", f"/project/{pid}/output/output.pdf/status"):
            resp = self._request("get", self._url(path), timeout=self.timeout)
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                continue
            data = self._json_or_empty(resp)
            if data:
                return self._compile_result(pid, data)
        if stored_outputs.get("pdf_url") or stored_outputs.get("log_url"):
            return CompileResult(
                project_id=pid,
                status="success",
                pdf_url=stored_outputs.get("pdf_url"),
                log_url=stored_outputs.get("log_url"),
                raw={"source": "cached_project_outputs"},
            )
        return None

    def download_pdf(self, project: str, output: Path) -> Path:
        pid = parse_project_id(project)
        stored_pdf_url = self.config.defaults.get("project_outputs", {}).get(pid, {}).get("pdf_url")
        candidates = []
        if stored_pdf_url:
            candidates.append(stored_pdf_url)
        status = self.compile_status(pid)
        if status and status.pdf_url:
            candidates.append(status.pdf_url)
        candidates.extend([
            f"/project/{pid}/output/output.pdf",
            f"/project/{pid}/output/output.pdf?compileGroup=standard",
        ])
        output.parent.mkdir(parents=True, exist_ok=True)
        last: requests.Response | None = None
        for path in candidates:
            resp = self._request("get", self._url_or_absolute(path), stream=True, timeout=max(self.timeout, 180))
            last = resp
            ctype = resp.headers.get("content-type", "")
            if resp.status_code == 200 and ("pdf" in ctype or str(path).split("?", 1)[0].endswith(".pdf")):
                with output.open("wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            fh.write(chunk)
                self.save_cookies()
                return output
        assert last is not None
        raise OverleafError(f"{self._error_text(last)}. Try `overleaf compile run {pid}` before downloading.")

    def fetch_log(self, project: str) -> str:
        pid = parse_project_id(project)
        stored_log_url = self.config.defaults.get("project_outputs", {}).get(pid, {}).get("log_url")
        candidates = []
        if stored_log_url:
            candidates.append(stored_log_url)
        candidates.extend([f"/project/{pid}/output/output.log", f"/project/{pid}/compile/log"])
        last: requests.Response | None = None
        for path in candidates:
            resp = self._request("get", self._url_or_absolute(path), timeout=self.timeout)
            last = resp
            if resp.status_code == 200 and resp.text:
                return resp.text
        assert last is not None
        raise OverleafError(self._error_text(last))

    def get_compiler(self, project: str) -> str | None:
        pid = parse_project_id(project)
        stored = self.config.defaults.get("project_settings", {}).get(pid, {}).get("compiler")
        if stored:
            return str(stored)
        try:
            return self._infer_compiler_from_log(self.fetch_log(pid))
        except OverleafError:
            return None

    def set_compiler(self, project: str, compiler: str) -> None:
        pid = parse_project_id(project)
        csrf = self.get_csrf()
        payloads = [
            (f"/project/{pid}/settings/compiler", {"compiler": compiler}),
            (f"/project/{pid}/settings", {"compiler": compiler}),
            (f"/project/{pid}/settings", {"key": "compiler", "value": compiler}),
        ]
        last: requests.Response | None = None
        for path, payload in payloads:
            resp = self._request(
                "post",
                self._url(path),
                json=payload,
                headers=self._csrf_headers(csrf, json_request=True),
                timeout=self.timeout,
            )
            last = resp
            if resp.status_code in {200, 204}:
                self._save_project_setting(pid, "compiler", compiler)
                self.save_cookies()
                return
            if resp.status_code in {404, 405}:
                continue
        assert last is not None
        raise OverleafError(self._error_text(last))

    def _infer_compiler_from_log(self, text: str) -> str | None:
        first_lines = "\n".join(text.splitlines()[:30]).lower()
        if "this is xetex" in first_lines:
            return "xelatex"
        if "this is luatex" in first_lines:
            return "lualatex"
        if "this is pdftex" in first_lines:
            return "pdflatex"
        if "this is tex" in first_lines:
            return "latex"
        return None

    def _compile_result(self, pid: str, data: dict) -> CompileResult:
        output_files = tuple(data.get("outputFiles") or data.get("output_files") or ())
        pdf_url = data.get("pdfUrl") or data.get("pdf_url")
        log_url = data.get("logUrl") or data.get("log_url")
        for item in output_files:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("path")
            file_type = str(item.get("type") or item.get("file") or item.get("name") or "")
            if url and (file_type == "pdf" or str(url).endswith(".pdf")):
                pdf_url = str(url)
            if url and str(url).endswith(".log"):
                log_url = str(url)
        return CompileResult(
            project_id=pid,
            status=str(data.get("status") or data.get("state") or "unknown"),
            pdf_url=pdf_url,
            log_url=log_url,
            output_files=output_files,
            raw=data,
        )

    def _create_folder_tree(self, pid: str, parts: list[str], known_folders: set[str]) -> str:
        if not parts:
            raise OverleafError("this Overleaf upload endpoint requires a target folder id; root upload is unsupported")
        parent_id: str | None = None
        current = ""
        for name in parts:
            current = f"{current}/{name}"
            if current in known_folders:
                if parent_id is None:
                    raise OverleafError(
                        f"remote folder already exists but this Overleaf endpoint does not expose its id: {current}"
                    )
                raise OverleafError(f"remote folder already exists in this session: {current}")
            data = self._create_folder_under(pid, name, parent_id)
            parent_id = self._entity_id(data)
            if not parent_id:
                raise OverleafError(f"folder creation response did not include an id: {data}")
            known_folders.add(current)
            self._cache_folder_id(pid, current, parent_id)
        assert parent_id is not None
        return parent_id

    def _create_folder_under(self, pid: str, name: str, parent_id: str | None) -> dict:
        csrf = self.get_csrf()
        payload = {"name": name}
        if parent_id:
            payload["parent_folder_id"] = parent_id
        resp = self._request(
            "post",
            self._url(f"/project/{pid}/folder"),
            data=payload,
            headers=self._csrf_headers(csrf),
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        self.save_cookies()
        return self._json_or_empty(resp)

    def _create_doc_under(self, pid: str, name: str, parent_id: str | None) -> dict:
        csrf = self.get_csrf()
        payload = {"name": name}
        if parent_id:
            payload["parent_folder_id"] = parent_id
        resp = self._request(
            "post",
            self._url(f"/project/{pid}/doc"),
            data=payload,
            headers=self._csrf_headers(csrf),
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        self.save_cookies()
        return self._json_or_empty(resp)

    def _delete_tree_entity(self, pid: str, entity_type: str, entity_id: str) -> None:
        endpoint = "file" if entity_type == "file" else entity_type
        csrf = self.get_csrf()
        resp = self._request(
            "delete",
            self._url(f"/project/{pid}/{endpoint}/{entity_id}"),
            headers=self._csrf_headers(csrf),
            timeout=self.timeout,
        )
        if resp.status_code not in {200, 204, 404}:
            raise OverleafError(self._error_text(resp))
        self.save_cookies()

    def _set_root_doc(self, pid: str, doc_id: str) -> None:
        csrf = self.get_csrf()
        resp = self._request(
            "post",
            self._url(f"/project/{pid}/settings"),
            json={"rootDocId": doc_id},
            headers=self._csrf_headers(csrf, json_request=True),
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        self.save_cookies()

    def _ensure_folder_for_text_upload(self, pid: str, parent: str, folders: dict[str, str]) -> str:
        parent = self._normalize_remote_path(parent, directory=True)
        if parent in folders:
            return folders[parent]
        current = ""
        parent_id = folders["/"]
        for part in self._remote_parts(parent):
            current = self._normalize_remote_path(posixpath.join(current, part), directory=True)
            if current in folders:
                parent_id = folders[current]
                continue
            data = self._create_folder_under(pid, part, parent_id)
            folder_id = self._entity_id(data)
            if not folder_id:
                raise OverleafError(f"folder creation response did not include an id: {data}")
            folders[current] = folder_id
            self._cache_folder_id(pid, current, folder_id)
            parent_id = folder_id
        return parent_id

    def _write_doc_text(self, pid: str, doc_id: str, text: str) -> None:
        socket = SocketIO09Client(self, pid)
        socket.connect()
        socket.poll()
        socket.wait_event("joinProjectResponse")
        joined = socket.emit_ack("joinDoc", [doc_id, {"encodeRanges": True}])
        if not joined or joined[0] is not None:
            raise OverleafError(f"joinDoc failed: {joined}")
        version = joined[2]
        socket.emit_ack("applyOtUpdate", [doc_id, {"v": version, "op": [{"p": 0, "i": text}]}])
        try:
            socket.emit_ack("leaveDoc", [doc_id], timeout=10)
        except OverleafError:
            pass
        try:
            self._request("post", self._url(f"/project/{pid}/flush"), timeout=self.timeout)
        except (OverleafError, requests.RequestException):
            pass

    def _upload_directory_contents(self, pid: str, local_dir: Path, folder_id: str, remote_dir: str) -> list[FileEntry]:
        uploaded: list[FileEntry] = []
        self._upload_directory_recursive(pid, local_dir, folder_id, remote_dir, uploaded)
        return uploaded

    def _upload_directory_recursive(
        self,
        pid: str,
        local_dir: Path,
        folder_id: str,
        remote_dir: str,
        uploaded: list[FileEntry],
    ) -> None:
        for path in sorted(item for item in local_dir.iterdir() if item.is_file()):
            data = self._upload_one_file(pid, path, folder_id, path.name)
            uploaded.append(
                FileEntry(
                    path=posixpath.join(remote_dir, path.name),
                    name=path.name,
                    type=str(data.get("entity_type") or "file"),
                    raw=data,
                )
            )
        for child in sorted(item for item in local_dir.iterdir() if item.is_dir()):
            data = self._create_folder_under(pid, child.name, folder_id)
            child_id = self._entity_id(data)
            if not child_id:
                raise OverleafError(f"folder creation response did not include an id: {data}")
            child_remote_dir = posixpath.join(remote_dir, child.name)
            self._cache_folder_id(pid, child_remote_dir, child_id)
            self._upload_directory_recursive(pid, child, child_id, child_remote_dir, uploaded)

    def _upload_one_file(self, pid: str, local_path: Path, folder_id: str, remote_relative_path: str) -> dict:
        csrf = self.get_csrf()
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        remote_name = posixpath.basename(remote_relative_path)
        with local_path.open("rb") as fh:
            files = {"qqfile": (remote_name, fh, content_type)}
            resp = self._request(
                "post",
                self._url(f"/project/{pid}/upload"),
                params={"folder_id": folder_id},
                data={"name": remote_name},
                files=files,
                headers=self._csrf_headers(csrf),
                timeout=max(self.timeout, 180),
            )
        if resp.status_code >= 400:
            raise OverleafError(self._error_text(resp))
        payload = self._json_or_empty(resp)
        if payload.get("success") is False:
            raise OverleafError(f"upload failed: {payload}")
        self.save_cookies()
        return payload

    def _folder_paths_from_entities(self, entity_paths: set[str]) -> set[str]:
        folders: set[str] = set()
        for path in entity_paths:
            parent = posixpath.dirname(path)
            while parent and parent != "/":
                folders.add(parent)
                parent = posixpath.dirname(parent)
        return folders

    def _remote_parts(self, remote_path: str) -> list[str]:
        path = self._normalize_remote_path(remote_path).strip("/")
        return [part for part in path.split("/") if part and part != "."]

    def _normalize_remote_path(self, remote_path: str, directory: bool = False) -> str:
        value = str(remote_path or "/").replace("\\", "/")
        if not value.startswith("/"):
            value = "/" + value
        normalized = posixpath.normpath(value)
        if normalized == ".":
            normalized = "/"
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        if directory and normalized != "/":
            normalized = normalized.rstrip("/")
        return normalized

    def _safe_join(self, root: Path, relative: str) -> Path:
        target = (root / relative).resolve()
        root_resolved = root.resolve()
        if target != root_resolved and root_resolved not in target.parents:
            raise OverleafError(f"unsafe zip path: {relative}")
        return target

    def _entity_id(self, data: dict) -> str | None:
        value = data.get("_id") or data.get("id") or data.get("entity_id")
        return str(value) if value else None

    def _cached_folder_id(self, pid: str, remote_path: str) -> str | None:
        folders = self.config.defaults.get("folder_ids", {}).get(pid, {})
        value = folders.get(self._normalize_remote_path(remote_path, directory=True))
        return str(value) if value else None

    def _cache_folder_id(self, pid: str, remote_path: str, folder_id: str) -> None:
        folder_ids = self.config.defaults.setdefault("folder_ids", {})
        project_folders = folder_ids.setdefault(pid, {})
        project_folders[self._normalize_remote_path(remote_path, directory=True)] = folder_id
        self.store.save(self.config)

    def _is_editable_doc_path(self, name: str) -> bool:
        return Path(name).suffix.lower() in {
            ".tex",
            ".bib",
            ".bst",
            ".cls",
            ".sty",
            ".txt",
            ".md",
            ".ltx",
            ".latex",
            ".tikz",
            ".rtex",
        }

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _url_or_absolute(self, path: str) -> str:
        return path if path.startswith("http://") or path.startswith("https://") else self._url(path)

    def _cache_bust_url(self, url: str) -> str:
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{urlencode({'_': int(time.time() * 1000)})}"

    def _save_output_url(self, project_id: str, key: str, value: str) -> None:
        outputs = self.config.defaults.setdefault("project_outputs", {})
        project_outputs = outputs.setdefault(project_id, {})
        project_outputs[key] = value
        self.store.save(self.config)

    def _save_project_setting(self, project_id: str, key: str, value: str) -> None:
        settings = self.config.defaults.setdefault("project_settings", {})
        project_settings = settings.setdefault(project_id, {})
        project_settings[key] = value
        self.store.save(self.config)

    def _filename_from_disposition(self, content_disposition: str | None) -> str | None:
        if not content_disposition:
            return None
        match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition)
        if match:
            return unquote(match.group(1).strip().strip('"'))
        match = re.search(r'filename="([^"]+)"', content_disposition) or re.search(r"filename=([^;]+)", content_disposition)
        if match:
            return match.group(1).strip()
        return None

    def _csrf_headers(self, csrf: str | None, json_request: bool = False) -> dict[str, str]:
        headers = {"Referer": self.project_url}
        if csrf:
            headers["X-Csrf-Token"] = csrf
            headers["X-CSRF-Token"] = csrf
        if json_request:
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "application/json"
        return headers

    def _project_id_from_response(self, resp: requests.Response, data: dict) -> str | None:
        pid = data.get("project_id") or data.get("projectId") or data.get("id")
        if pid:
            try:
                return parse_project_id(str(pid))
            except ValueError:
                pass
        redirect = resp.headers.get("Location") or data.get("redirect")
        if redirect:
            try:
                return parse_project_id(str(redirect))
            except ValueError:
                pass
        if "/project/" in resp.url:
            try:
                return parse_project_id(resp.url)
            except ValueError:
                pass
        return None

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        self._ensure_authenticated_locally()
        resp = self.session.request(method, url, **kwargs)
        self._raise_for_auth(resp)
        return resp

    def _ensure_authenticated_locally(self) -> None:
        if not has_any_cookie(self.session.cookies):
            raise AuthRequired("not logged in; run `overleaf auth login`")

    def _raise_for_auth(self, resp: requests.Response) -> None:
        if resp.status_code in {401, 403} or self._looks_like_login(resp):
            self.session.cookies.clear()
            self.store.clear_cookies()
            raise AuthRequired("session is not authenticated; run `overleaf auth login`")

    def _looks_like_login(self, resp: requests.Response) -> bool:
        parsed = urlparse(resp.url)
        text = resp.text[:3000].lower() if resp.text else ""
        return (
            "jaccount" in parsed.netloc.lower()
            or "jaccount" in parsed.path.lower()
            or "统一身份认证" in resp.text[:3000]
            or ("login" in parsed.path.lower() and "/project" not in parsed.path.lower())
            or ("password" in text and "login" in text and "project" not in parsed.path.lower())
        )

    def _json_or_empty(self, resp: requests.Response) -> dict:
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {"data": data}

    def _error_text(self, resp: requests.Response) -> str:
        text = resp.text.strip().replace("\n", " ")
        if len(text) > 500:
            text = text[:500] + "..."
        return f"HTTP {resp.status_code} from {resp.url}: {text}"


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _errno_is_success(value: object) -> bool:
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False


def _bool_or_none(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def _verification_success(data: dict, entity: dict) -> bool:
    if "success" in entity:
        parsed = _bool_or_none(entity.get("success"))
        if parsed is not None:
            return parsed
    if "errno" in data:
        return _errno_is_success(data.get("errno"))
    return False


def _normalize_verification_code(code: str) -> str:
    return re.sub(r"[\s\-\u2010-\u2015\u2212]+", "", code)
