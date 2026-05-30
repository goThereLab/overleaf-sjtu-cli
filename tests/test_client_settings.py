from pathlib import Path

import pytest
import requests

from overleaf_sjtu.client import AuthRequired, OverleafClient
from overleaf_sjtu.config import Config, ConfigStore
from overleaf_sjtu.models import Project


def test_infer_compiler_from_log(tmp_path: Path) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))

    assert client._infer_compiler_from_log("This is XeTeX, Version ...") == "xelatex"
    assert client._infer_compiler_from_log("This is LuaTeX, Version ...") == "lualatex"
    assert client._infer_compiler_from_log("This is pdfTeX, Version ...") == "pdflatex"


def test_compile_can_send_temporary_compiler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    seen_payloads = []

    monkeypatch.setattr(client, "get_csrf", lambda: "csrf")

    def fake_request(method, url, **kwargs):
        seen_payloads.append(kwargs.get("json"))
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = b'{"status":"success"}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.compile("0123456789abcdefabcdefab", compiler="xelatex")

    assert result.status == "success"
    assert seen_payloads == [{"draft": False, "check": "silent", "stopOnFirstError": False, "compiler": "xelatex"}]


def test_whoami_counts_the_same_project_list(tmp_path: Path) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    client.list_projects = lambda: [
        Project(id="0123456789abcdefabcdefab", name="A"),
        Project(id="fedcba987654321001234567", name="B"),
    ]

    info = client.whoami()

    assert info["authenticated"] is True
    assert info["project_count_visible"] == 2


def test_whoami_uses_cached_nonzero_count_when_project_prefetch_is_empty(tmp_path: Path) -> None:
    config = Config(defaults={"last_project_count_visible": 3})
    client = OverleafClient(config, ConfigStore(tmp_path / "config.json"))
    client.list_projects = lambda: []

    info = client.whoami()

    assert info["project_count_visible"] == 3


def test_list_projects_retries_empty_prefetch_with_cache_bust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config(defaults={"last_project_count_visible": 3})
    client = OverleafClient(config, ConfigStore(tmp_path / "config.json"))
    client.session.cookies.set("overleaf.sid", "ok", domain="latex.sjtu.edu.cn", path="/")
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((url, kwargs.get("headers")))
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        if len(seen) == 1:
            resp._content = b'<meta name="ol-prefetchedProjectsBlob" data-type="json" content="{&quot;projects&quot;:[]}">'
        else:
            resp._content = (
                b'<meta name="ol-prefetchedProjectsBlob" data-type="json" '
                b'content="{&quot;projects&quot;:[{&quot;id&quot;:&quot;0123456789abcdefabcdefab&quot;,&quot;name&quot;:&quot;A&quot;}]}">'
            )
        return resp

    monkeypatch.setattr(client.session, "request", fake_request)

    projects = client.list_projects()

    assert [project.id for project in projects] == ["0123456789abcdefabcdefab"]
    assert len(seen) == 2
    assert "_=" in seen[1][0]
    assert seen[1][1]["Cache-Control"] == "no-cache"


def test_auth_required_short_circuits_without_local_cookies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    client.session.cookies.clear()

    def fail_request(*args, **kwargs):
        raise AssertionError("network should not be called without cookies")

    monkeypatch.setattr(client.session, "request", fail_request)

    with pytest.raises(AuthRequired, match="overleaf auth login"):
        client.list_projects()


def test_detects_jaccount_additional_verification(tmp_path: Path) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    resp = requests.Response()
    resp.status_code = 200
    resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    resp._content = (
        b'<form method="get">'
        b'<input name="shouldauth" value="true">'
        b'<input name="c" value="app">'
        b'<input name="c" value="email">'
        b'<input name="c" value="sms">'
        b'<input name="captcha">'
        b"</form>"
    )

    assert client._looks_like_jaccount_verification(resp) is True
    challenge = client._jaccount_verification_challenge(resp)
    assert challenge is not None
    assert challenge.methods == ["app", "email", "sms"]


@pytest.mark.parametrize(
    ("account_html", "expected"),
    [
        ('<script>account: "hammer"</script>', "hammer"),
        ('<script>account = "hammershock"</script>', "hammershock"),
        ('<script>{"account":"zhanghanmo"}</script>', "zhanghanmo"),
        ('<input name="account" value="jaccount-user">', "jaccount-user"),
    ],
)
def test_detects_jaccount_additional_verification_account_variants(
    account_html: str,
    expected: str,
    tmp_path: Path,
) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    resp = requests.Response()
    resp.status_code = 200
    resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    resp._content = f"""
    {account_html}
    <form>
      <input name="shouldauth" value="true">
      <input name="c" value="email">
      <input name="captcha">
    </form>
    """.encode()

    challenge = client._jaccount_verification_challenge(resp)

    assert challenge is not None
    assert challenge.account == expected


@pytest.mark.parametrize("method", ["app", "email", "sms"])
def test_requests_and_submits_jaccount_verification(method: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = b"""
    <script>account: 'hammer'</script>
    <form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>
    """
    seen = []

    def fake_post(url, data=None, **kwargs):
        seen.append((url, data, kwargs.get("headers", {}).get("Referer")))
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        if url.endswith("/2fa/loginVerify"):
            resp._content = f'{{"errno":0,"entities":[{{"success":true,"msg":"sent to {method}","retrySeconds":60}}]}}'.encode()
        else:
            resp._content = b'{"errno":0,"error":"ok"}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    monkeypatch.setattr(client.session, "post", fake_post)

    sent = client.request_jaccount_verification(challenge_resp, method)
    verified = client.submit_jaccount_verification(challenge_resp, "123456")

    assert sent.success is True
    assert sent.message == f"sent to {method}"
    assert sent.retry_seconds == 60
    assert verified.success is True
    assert seen[0][0].endswith("/jaccount/2fa/loginVerify")
    assert seen[0][1] == {"c": method}
    assert seen[1][0].endswith("/jaccount/2faVerify")
    assert seen[1][1] == {"account": "hammer", "captcha": "123456", "trust": "true"}


def test_jaccount_verification_request_parses_string_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = b'<form><input name="shouldauth" value="true"><input name="c" value="sms"><input name="captcha"></form>'

    def fake_post(url, data=None, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = b'{"errno":0,"entities":[{"success":"false","msg":"Request too often","retrySeconds":"60"}]}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    monkeypatch.setattr(client.session, "post", fake_post)

    sent = client.request_jaccount_verification(challenge_resp, "sms")

    assert sent.success is False
    assert sent.message == "Request too often"
    assert sent.retry_seconds == 60


def test_jaccount_verification_request_requires_success_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = b'<form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>'

    def fake_post(url, data=None, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = b"<html>login expired</html>"
        resp.headers["Content-Type"] = "text/html"
        return resp

    monkeypatch.setattr(client.session, "post", fake_post)

    sent = client.request_jaccount_verification(challenge_resp, "email")

    assert sent.success is False
    assert sent.message == "verification code request did not return a success flag"


def test_jaccount_verification_submit_accepts_string_errno(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = (
        b"<script>account: 'hammer'</script>"
        b'<form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>'
    )

    def fake_post(url, data=None, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = b'{"errno":"0","error":"ok"}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    monkeypatch.setattr(client.session, "post", fake_post)

    verified = client.submit_jaccount_verification(challenge_resp, "123456")

    assert verified.success is True


def test_jaccount_verification_submit_normalizes_code_separators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = (
        b"<script>account: 'hammer'</script>"
        b'<form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>'
    )
    seen = {}

    def fake_post(url, data=None, **kwargs):
        seen.update(data=data)
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = b'{"errno":0,"error":"ok"}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    monkeypatch.setattr(client.session, "post", fake_post)

    verified = client.submit_jaccount_verification(challenge_resp, "123- 45—6\n")

    assert verified.success is True
    assert seen["data"]["captcha"] == "123456"


def test_jaccount_verification_submit_rejects_empty_code_after_normalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = (
        b"<script>account: 'hammer'</script>"
        b'<form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>'
    )

    def fake_post(url, data=None, **kwargs):
        raise AssertionError("empty verification code should not be submitted")

    monkeypatch.setattr(client.session, "post", fake_post)

    with pytest.raises(AuthRequired, match="verification code is empty"):
        client.submit_jaccount_verification(challenge_resp, " - — \n")


def test_jaccount_verification_submit_requires_success_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = (
        b"<script>account: 'hammer'</script>"
        b'<form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>'
    )

    def fake_post(url, data=None, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = b"<html>login expired</html>"
        resp.headers["Content-Type"] = "text/html"
        return resp

    monkeypatch.setattr(client.session, "post", fake_post)

    verified = client.submit_jaccount_verification(challenge_resp, "123456")

    assert verified.success is False
    assert verified.message == "verification submit did not return a success flag"


def test_complete_jaccount_verification_detects_remaining_challenge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    challenge_resp = requests.Response()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
    challenge_resp._content = (
        b"<script>account: 'hammer'</script>"
        b'<form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>'
    )

    def fake_post(url, data=None, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = b'{"errno":0,"error":"ok"}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    def fake_get(url, **kwargs):
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp._content = challenge_resp.content
        return resp

    monkeypatch.setattr(client.session, "post", fake_post)
    monkeypatch.setattr(client.session, "get", fake_get)

    with pytest.raises(AuthRequired, match="additional verification did not complete"):
        client.complete_jaccount_verification(challenge_resp, "email", "123456", request_code=False)


def test_login_with_jaccount_mfa_code_does_not_resend_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    login_state = {
        "post_url": "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
        "login_url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
        "context": {"sid": "sid", "client": "client", "returl": "ret", "se": "se", "v": "", "uuid": "uuid"},
    }
    seen_posts = []

    def fake_post(url, data=None, **kwargs):
        seen_posts.append((url, data))
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        resp.headers["Content-Type"] = "application/json"
        if url.endswith("/ulogin"):
            resp._content = b'{"errno":0,"url":"/jaccount/jalogin?ok=1"}'
        elif url.endswith("/2faVerify"):
            resp._content = b'{"errno":0,"error":"ok"}'
        else:
            raise AssertionError(f"unexpected post: {url}")
        return resp

    seen_gets = []

    def fake_get(url, **kwargs):
        seen_gets.append(url)
        resp = requests.Response()
        resp.status_code = 200
        resp.url = url
        if len(seen_gets) == 1:
            resp._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true"><input name="c" value="email"><input name="captcha"></form>'
            )
        else:
            resp._content = b"<html>ok</html>"
        return resp

    whoami_calls = []

    monkeypatch.setattr(client.session, "post", fake_post)
    monkeypatch.setattr(client.session, "get", fake_get)
    monkeypatch.setattr(client, "whoami", lambda: whoami_calls.append(True) or {"project_count_visible": 3})

    info = client.login_with_jaccount(
        username="hammer",
        password="secret",
        captcha="abcd",
        login_state=login_state,
        mfa_method="email",
        mfa_code="123456",
    )

    assert info == {"project_count_visible": 3}
    assert [url.rsplit("/", 1)[-1] for url, data in seen_posts] == ["ulogin", "2faVerify"]
    assert seen_posts[1][1] == {"account": "hammer", "captcha": "123456", "trust": "true"}
    assert len(whoami_calls) == 1


def test_http_auth_status_maps_to_auth_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ConfigStore(tmp_path / "config.json")
    client = OverleafClient(Config(), store)
    client.session.cookies.set("overleaf.sid", "expired", domain="latex.sjtu.edu.cn", path="/")
    client.save_cookies()
    resp = requests.Response()
    resp.status_code = 401
    resp.url = "https://latex.sjtu.edu.cn/project"
    resp._content = b"Unauthorized"

    monkeypatch.setattr(client.session, "request", lambda *args, **kwargs: resp)

    with pytest.raises(AuthRequired, match="overleaf auth login"):
        client.list_projects()
    assert not store.cookie_path.exists()
    assert not list(client.session.cookies)


def test_create_project_posts_blank_project_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = OverleafClient(Config(), ConfigStore(tmp_path / "config.json"))
    client.session.cookies.set("overleaf.sid", "ok", domain="latex.sjtu.edu.cn", path="/")
    seen = []

    def fake_request(method, url, **kwargs):
        seen.append((method, url, kwargs))
        resp = requests.Response()
        resp.url = url
        if url.endswith("/project"):
            resp.status_code = 200
            resp._content = b'<meta name="ol-csrfToken" content="csrf-1">'
            return resp
        resp.status_code = 200
        resp._content = b'{"project_id":"0123456789abcdefabcdefab"}'
        resp.headers["Content-Type"] = "application/json"
        return resp

    monkeypatch.setattr(client.session, "request", fake_request)

    project = client.create_project("Blank Paper")

    assert project.id == "0123456789abcdefabcdefab"
    assert project.name == "Blank Paper"
    assert seen[1][0] == "post"
    assert seen[1][1].endswith("/project/new")
    assert seen[1][2]["data"]["projectName"] == "Blank Paper"
    assert seen[1][2]["data"]["_csrf"] == "csrf-1"
