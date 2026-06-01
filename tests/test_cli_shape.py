import json

import requests
import pytest
from typer.testing import CliRunner

from overleaf_sjtu.client import AuthRequired, JAccountVerificationChallenge, JAccountVerificationRequired, JAccountVerificationResult
from overleaf_sjtu.cli import _save_captcha_if_needed, app
from overleaf_sjtu.models import CompileResult


runner = CliRunner()


def test_auth_commands_are_grouped() -> None:
    root = runner.invoke(app, ["--help"])
    auth = runner.invoke(app, ["auth"])

    assert root.exit_code == 0
    assert "auth" in root.output
    assert "completion" not in root.output
    assert "login" not in [line.split()[0] for line in root.output.splitlines() if line.strip()]
    assert auth.exit_code == 0
    assert "login" in auth.output
    assert "logout" in auth.output
    assert "whoami" in auth.output
    assert "pending" in auth.output


def test_login_help_exposes_jaccount_mfa_controls() -> None:
    result = runner.invoke(app, ["auth", "login", "--help"])

    assert result.exit_code == 0
    assert "--mfa-method [app|email|sms]" in result.output
    assert "--mfa-code TEXT" in result.output
    assert "--mfa-resend" in result.output
    assert "--trust-mfa / --no-trust-mfa" in result.output


def test_auth_flow_start_saves_explicit_flow(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def begin_jaccount_login(self):
            return {
                "login_url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                "post_url": "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
                "captcha_url": "https://jaccount.sjtu.edu.cn/jaccount/captcha",
                "requires_captcha": True,
                "context": {"uuid": "abc"},
            }

        def get_login_captcha(self, login_state):
            return b"png"

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)
    flow = tmp_path / "login-flow.json"
    captcha = tmp_path / "captcha.png"

    result = runner.invoke(
        app,
        ["auth", "flow", "start", "--flow", str(flow), "--captcha-output", str(captcha), "--json"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["flow"] == str(flow)
    assert data["state"] == "captcha_required"
    assert data["captcha_path"] == str(captcha)
    assert captcha.read_bytes() == b"png"
    assert flow.exists()
    assert flow.stat().st_mode & 0o777 == 0o600


def test_auth_flow_password_mfa_and_submit(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()
            self.session.cookies.set("JAAuthCookie", "ok", domain="jaccount.sjtu.edu.cn", path="/")

        def login_with_jaccount(self, **kwargs):
            assert kwargs["username"] == "hammer"
            assert kwargs["password"] == "secret"
            assert kwargs["captcha"] == "ianck"
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true">'
                b'<input name="c" value="email"><input name="c" value="sms"><input name="captcha"></form>'
            )
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["email", "sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            assert method == "email"
            return JAccountVerificationResult(success=True, message="sent to email", retry_seconds=60)

        def complete_jaccount_verification(self, response, method, code, trust=True, request_code=False, account=None):
            assert method == "email"
            assert code == "123456"
            assert account == "hammer"
            return {"project_count_visible": 2}

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)
    flow = tmp_path / "login-flow.json"
    flow.write_text(
        json.dumps(
            {
                "created_at": 4102444800,
                "captcha_path": str(tmp_path / "captcha.png"),
                "login_state": {"requires_captcha": True, "context": {"uuid": "abc"}},
            }
        )
    )
    flow.chmod(0o600)
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}

    password = runner.invoke(
        app,
        [
            "auth",
            "flow",
            "submit-password",
            "--flow",
            str(flow),
            "--username",
            "hammer",
            "--password",
            "secret",
            "--captcha",
            "ianck",
            "--json",
        ],
        env=env,
    )
    assert password.exit_code == 0
    password_data = json.loads(password.output)
    assert password_data["state"] == "mfa_required"
    assert password_data["methods"] == ["email", "sms"]
    assert "secret" not in flow.read_text()

    request = runner.invoke(app, ["auth", "flow", "mfa-request", "--flow", str(flow), "--method", "email", "--json"], env=env)
    assert request.exit_code == 0
    request_data = json.loads(request.output)
    assert request_data["state"] == "mfa_code_requested"
    assert request_data["retry_seconds"] == 60

    submit = runner.invoke(app, ["auth", "flow", "mfa-submit", "--flow", str(flow), "--code", "123456", "--json"], env=env)
    assert submit.exit_code == 0
    submit_data = json.loads(submit.output)
    assert submit_data["state"] == "authenticated"
    assert submit_data["project_count_visible"] == 2
    assert not flow.exists()


def test_compile_run_help_explains_compiler_choice() -> None:
    result = runner.invoke(app, ["compile", "run", "--help"])
    normalized = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "--compiler [latex|lualatex|pdflatex|xelatex]" in result.output
    assert "overleaf settings compiler [latex|lualatex|pdflatex|xelatex]" in normalized


def test_compile_run_prints_and_passes_compiler_override(monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            pass

        def resolve_project(self, project):
            return "0123456789abcdefabcdefab"

        def get_compiler(self, project):
            raise AssertionError("explicit compiler should not read current compiler")

        def compile(self, project, **kwargs):
            seen.update(kwargs)
            return CompileResult(project_id=project, status="success", pdf_url="/output.pdf")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["compile", "run", "--compiler", "xelatex"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    assert "0123456789abcdefabcdefab: compiler=xelatex (override)" in result.output
    assert seen["compiler"] == "xelatex"


def test_compile_run_prints_current_compiler(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            pass

        def resolve_project(self, project):
            return "0123456789abcdefabcdefab"

        def get_compiler(self, project):
            return "pdflatex"

        def compile(self, project, **kwargs):
            assert kwargs["compiler"] is None
            return CompileResult(project_id=project, status="success")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["compile", "run"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    assert "0123456789abcdefabcdefab: compiler=pdflatex (setting)" in result.output


def test_logout_can_forget_keyring_credentials(monkeypatch) -> None:
    deleted = []

    def fake_delete(username=None):
        deleted.append(username)
        return ["canvas:jaccount.username", "canvas:jaccount.password:hammer"]

    monkeypatch.setattr("overleaf_sjtu.cli.delete_saved_credentials", fake_delete)

    result = runner.invoke(app, ["auth", "logout", "--forget-credentials", "--username", "hammer", "--yes"])

    assert result.exit_code == 0
    assert deleted == ["hammer"]
    assert "deleted 2 keyring entries" in result.output


def test_logout_clears_pending_jaccount_verification(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text('{"created_at": 4102444800, "mfa_state": {"method": "email"}}')

    result = runner.invoke(app, ["auth", "logout"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert not pending.exists()


def test_auth_pending_reports_mfa_state(tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "method": "email",
                    "methods": ["app", "email", "sms"],
                    "account": "hammer",
                },
            }
        )
    )

    result = runner.invoke(app, ["auth", "pending"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert "Pending jAccount additional verification: method=email" in result.output
    assert "available methods: app, email, sms" in result.output
    assert "overleaf auth login --mfa-code CODE --no-remember" in result.output
    assert "overleaf auth login --mfa-resend" in result.output


def test_auth_pending_json_redacts_mfa_state_secrets(tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<html>secret challenge</html>",
                    "method": "email",
                    "methods": ["email"],
                    "account": "hammer",
                    "cookies": [{"name": "JAAuthCookie", "value": "secret-cookie"}],
                },
            }
        )
    )

    result = runner.invoke(app, ["auth", "pending", "--json"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert '"type": "mfa"' in result.output
    assert '"method": "email"' in result.output
    assert "secret-cookie" not in result.output
    assert "secret challenge" not in result.output
    assert "JAAuthCookie" not in result.output


def test_auth_pending_json_reports_methodless_mfa_state(tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<html>secret challenge</html>",
                    "methods": ["email", "sms"],
                    "account": "hammer",
                    "cookies": [{"name": "JAAuthCookie", "value": "secret-cookie"}],
                },
            }
        )
    )

    result = runner.invoke(app, ["auth", "pending", "--json"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert '"type": "mfa"' in result.output
    assert '"method": null' in result.output
    assert '"methods": [' in result.output
    assert '"email"' in result.output
    assert '"sms"' in result.output
    assert "secret-cookie" not in result.output
    assert "secret challenge" not in result.output


def test_auth_pending_reports_captcha_state(tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "captcha_path": str(tmp_path / "captcha.png"),
                "login_state": {"requires_captcha": True},
            }
        )
    )

    result = runner.invoke(app, ["auth", "pending"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert "Pending jAccount CAPTCHA" in result.output
    assert str(tmp_path / "captcha.png") in result.output
    assert "overleaf auth login --username USERNAME --password PASSWORD --captcha CAPTCHA --no-remember" in result.output


def test_auth_pending_json_redacts_captcha_login_context(tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "captcha_path": str(tmp_path / "captcha.png"),
                "mfa_method": "email",
                "login_state": {
                    "requires_captcha": True,
                    "context": {"sid": "secret-sid", "uuid": "secret-uuid"},
                    "post_url": "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
                },
            }
        )
    )

    result = runner.invoke(app, ["auth", "pending", "--json"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert '"type": "captcha"' in result.output
    assert '"mfa_method": "email"' in result.output
    assert "secret-sid" not in result.output
    assert "secret-uuid" not in result.output
    assert "ulogin" not in result.output


def test_auth_pending_reports_captcha_state_with_mfa_method(tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "captcha_path": str(tmp_path / "captcha.png"),
                "mfa_method": "sms",
                "login_state": {"requires_captcha": True},
            }
        )
    )

    result = runner.invoke(app, ["auth", "pending"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert "overleaf auth login --username USERNAME --password PASSWORD --captcha CAPTCHA --mfa-method sms --no-remember" in result.output


def test_auth_pending_json_expires_old_state(tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text('{"created_at": 1, "mfa_state": {"method": "email"}}')

    result = runner.invoke(app, ["auth", "pending", "--json"], env={"XDG_STATE_HOME": str(tmp_path / "state")})

    assert result.exit_code == 0
    assert '"pending": false' in result.output
    assert '"expired": true' in result.output
    assert not pending.exists()


def test_login_reuses_saved_credentials_without_resaving(monkeypatch) -> None:
    saved = []

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, username, password, captcha=None, login_state=None):
            assert username == "hammer"
            assert password == "secret"
            return {"project_count_visible": 3}

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)
    monkeypatch.setattr("overleaf_sjtu.cli.get_saved_credentials", lambda: ("hammer", "secret"))
    monkeypatch.setattr("overleaf_sjtu.cli.save_credentials", lambda username, password: saved.append((username, password)))

    result = runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 0
    assert "Logged in: 3 visible projects" in result.output
    assert saved == []


def test_login_explicit_remember_saves_entered_credentials(monkeypatch) -> None:
    saved = []

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, username, password, captcha=None, login_state=None):
            assert username == "hammer"
            assert password == "secret"
            return {"project_count_visible": 3}

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)
    monkeypatch.setattr("overleaf_sjtu.cli.get_saved_credentials", lambda: (None, None))
    monkeypatch.setattr("overleaf_sjtu.cli.getpass.getpass", lambda prompt: "secret")
    monkeypatch.setattr("overleaf_sjtu.cli.save_credentials", lambda username, password: saved.append((username, password)))

    result = runner.invoke(app, ["auth", "login", "--remember"], input="hammer\n")

    assert result.exit_code == 0
    assert saved == [("hammer", "secret")]


def test_login_without_tty_stages_captcha_before_credentials(monkeypatch, tmp_path) -> None:
    called = {"login": False}
    login_state = {
        "login_url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
        "post_url": "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
        "captcha_url": "https://jaccount.sjtu.edu.cn/jaccount/captcha",
        "requires_captcha": True,
        "context": {"uuid": "uuid-1"},
    }

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            return login_state

        def get_login_captcha(self, state):
            assert state["context"]["uuid"] == "uuid-1"
            return b"png"

        def login_with_jaccount(self, *args, **kwargs):
            called["login"] = True
            raise AssertionError("first non-TTY captcha stage should not submit credentials")

    output = tmp_path / "captcha.png"
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--captcha-output", str(output)],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    assert output.read_bytes() == b"png"
    assert "Next:" in result.output
    assert "overleaf auth login --username USERNAME --password PASSWORD --captcha CAPTCHA --no-remember" in result.output
    assert called["login"] is False
    assert (tmp_path / "state" / "overleaf-sjtu" / "login_state.json").exists()


def test_login_without_tty_captcha_hint_keeps_mfa_method(monkeypatch, tmp_path) -> None:
    called = {"login": False}
    login_state = {
        "login_url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
        "post_url": "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
        "captcha_url": "https://jaccount.sjtu.edu.cn/jaccount/captcha",
        "requires_captcha": True,
        "context": {"uuid": "uuid-1"},
    }

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            return login_state

        def get_login_captcha(self, state):
            assert state["context"]["uuid"] == "uuid-1"
            return b"png"

        def login_with_jaccount(self, *args, **kwargs):
            called["login"] = True
            raise AssertionError("first non-TTY captcha stage should not submit credentials")

    output = tmp_path / "captcha.png"
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--captcha-output", str(output), "--mfa-method", "email"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    assert output.read_bytes() == b"png"
    assert "Next:" in result.output
    assert (
        "overleaf auth login --username USERNAME --password PASSWORD --captcha CAPTCHA "
        "--mfa-method email --no-remember"
    ) in result.output
    assert called["login"] is False
    assert '"mfa_method": "email"' in (tmp_path / "state" / "overleaf-sjtu" / "login_state.json").read_text()


def test_login_with_captcha_reuses_staged_login_state(monkeypatch, tmp_path) -> None:
    seen = {}
    staged = {
        "created_at": 4102444800,
        "captcha_path": str(tmp_path / "captcha.png"),
        "login_state": {
            "login_url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
            "post_url": "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
            "captcha_url": "https://jaccount.sjtu.edu.cn/jaccount/captcha",
            "requires_captcha": True,
            "context": {"uuid": "uuid-staged"},
        },
    }
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    (state_dir / "login_state.json").write_text(__import__("json").dumps(staged))

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            raise AssertionError("staged login state should be reused")

        def login_with_jaccount(self, username, password, captcha=None, login_state=None):
            seen.update(username=username, password=password, captcha=captcha, uuid=login_state["context"]["uuid"])
            return {"project_count_visible": 3}

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--captcha", "abcd", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    assert seen == {"username": "hammer", "password": "secret", "captcha": "abcd", "uuid": "uuid-staged"}
    assert not (state_dir / "login_state.json").exists()


def test_login_with_captcha_reuses_staged_mfa_method(monkeypatch, tmp_path) -> None:
    seen = {}
    staged = {
        "created_at": 4102444800,
        "captcha_path": str(tmp_path / "captcha.png"),
        "mfa_method": "sms",
        "login_state": {
            "login_url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
            "post_url": "https://jaccount.sjtu.edu.cn/jaccount/ulogin",
            "captcha_url": "https://jaccount.sjtu.edu.cn/jaccount/captcha",
            "requires_captcha": True,
            "context": {"uuid": "uuid-staged"},
        },
    }
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    (state_dir / "login_state.json").write_text(__import__("json").dumps(staged))

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            raise AssertionError("staged login state should be reused")

        def login_with_jaccount(self, username, password, captcha=None, login_state=None, **kwargs):
            seen.update(mfa_method=kwargs.get("mfa_method"), captcha=captcha, uuid=login_state["context"]["uuid"])
            return {"project_count_visible": 3}

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--captcha", "abcd", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    assert seen == {"mfa_method": "sms", "captcha": "abcd", "uuid": "uuid-staged"}


def test_login_with_captcha_requires_staged_login_state(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            raise AssertionError("should not start a new login context for a supplied captcha")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--captcha", "abcd", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "captcha login state not found" in str(result.exception)


def test_login_with_mfa_code_requires_pending_state_even_with_credentials(monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = type("Response", (), {"url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin"})()
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["app", "email", "sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            seen["method"] = method
            return JAccountVerificationResult(success=True, message="sent")

        def complete_jaccount_verification(self, response, method, code, trust=True, request_code=True, account=None):
            seen.update(complete_method=method, code=code, trust=trust, request_code=request_code, account=account)
            return {"project_count_visible": 3}

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "auth",
            "login",
            "--username",
            "hammer",
            "--password",
            "secret",
            "--mfa-method",
            "email",
            "--mfa-code",
            "123456",
            "--no-remember",
        ],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "pending jAccount additional verification state not found or expired" in str(result.exception)
    assert seen == {}


@pytest.mark.parametrize("method", ["app", "email", "sms"])
def test_login_stages_and_resumes_jaccount_additional_verification(method, monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()
            self.session.cookies.set("JAAuthCookie", "ok", domain="jaccount.sjtu.edu.cn", path="/")

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true">'
                b'<input name="c" value="app"><input name="c" value="email"><input name="c" value="sms">'
                b'<input name="captcha"></form>'
            )
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["app", "email", "sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            seen["request_method"] = method
            return JAccountVerificationResult(success=True, message="sent", retry_seconds=60)

        def complete_jaccount_verification(self, response, method, code, trust=True, request_code=True, account=None):
            seen.update(complete_url=response.url, complete_method=method, code=code, trust=trust, request_code=request_code, account=account)
            return {"project_count_visible": 3}

    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    first = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--mfa-method", method, "--no-remember"],
        env=env,
    )
    second = runner.invoke(app, ["auth", "login", "--mfa-code", "123456", "--no-trust-mfa", "--no-remember"], env=env)

    assert first.exit_code == 0
    assert "Next:" in first.output
    assert "Resend available in 60 seconds" in first.output
    assert "--mfa-code CODE" in first.output
    assert second.exit_code == 0
    assert "Logged in: 3 visible projects" in second.output
    assert seen == {
        "request_method": method,
        "complete_url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
        "complete_method": method,
        "code": "123456",
        "trust": False,
        "request_code": False,
        "account": "hammer",
    }


def test_login_stages_jaccount_verification_without_method_and_selects_later(monkeypatch, tmp_path) -> None:
    seen = {}

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()
            self.session.cookies.set("JAAuthCookie", "ok", domain="jaccount.sjtu.edu.cn", path="/")

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true">'
                b'<input name="c" value="app"><input name="c" value="email"><input name="c" value="sms">'
                b'<input name="captcha"></form>'
            )
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["app", "email", "sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            seen["request_method"] = method
            return JAccountVerificationResult(success=True, message="sent", retry_seconds=60)

    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    state_path = tmp_path / "state" / "overleaf-sjtu" / "login_state.json"
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    first = runner.invoke(app, ["auth", "login", "--username", "hammer", "--password", "secret", "--no-remember"], env=env)
    pending = runner.invoke(app, ["auth", "pending"], env=env)
    second = runner.invoke(app, ["auth", "login", "--mfa-method", "email", "--no-remember"], env=env)

    assert first.exit_code == 0
    assert "choose METHOD from: app, email, sms" in first.output
    assert "method=not selected" in pending.output
    assert second.exit_code == 0
    assert "sent" in second.output
    assert "--mfa-code CODE" in second.output
    assert seen == {"request_method": "email"}
    assert '"method": "email"' in state_path.read_text()


def test_login_rejects_unavailable_method_for_methodless_pending_jaccount_verification(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<script>account: 'hammer'</script><form><input name='shouldauth' value='true'><input name='c' value='email'><input name='c' value='sms'><input name='captcha'></form>",
                    "account": "hammer",
                    "methods": ["email", "sms"],
                    "cookies": [],
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def request_jaccount_verification(self, response, method):
            raise AssertionError("unavailable method should be rejected before request")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-method", "app", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "method app is not available; choose one of: email, sms" in str(result.exception)
    assert '"method"' not in pending.read_text()


def test_login_with_methodless_pending_jaccount_verification_rejects_code_before_method(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<script>account: 'hammer'</script><form><input name='shouldauth' value='true'><input name='c' value='email'><input name='c' value='sms'><input name='captcha'></form>",
                    "account": "hammer",
                    "methods": ["email", "sms"],
                    "cookies": [],
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def complete_jaccount_verification(self, *args, **kwargs):
            raise AssertionError("methodless pending MFA should not submit code")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-code", "123456", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "no selected method; rerun with --mfa-method one of: email, sms" in str(result.exception)
    assert '"method"' not in pending.read_text()


def test_login_keeps_methodless_pending_jaccount_verification_after_failed_method_selection(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<script>account: 'hammer'</script><form><input name='shouldauth' value='true'><input name='c' value='email'><input name='captcha'></form>",
                    "account": "hammer",
                    "methods": ["email"],
                    "cookies": [],
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def request_jaccount_verification(self, response, method):
            return JAccountVerificationResult(success=False, message="Request too often", retry_seconds=60)

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-method", "email", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "Resend available in 60 seconds" in result.output
    assert "Request too often" in str(result.exception)
    assert '"method"' not in pending.read_text()


def test_login_keeps_pending_jaccount_verification_after_bad_code(monkeypatch, tmp_path) -> None:
    seen = {"requests": 0, "codes": []}

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()
            self.session.cookies.set("JAAuthCookie", "ok", domain="jaccount.sjtu.edu.cn", path="/")

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true">'
                b'<input name="c" value="email"><input name="captcha"></form>'
            )
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["email"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            seen["requests"] += 1
            return JAccountVerificationResult(success=True, message="sent")

        def complete_jaccount_verification(self, response, method, code, trust=True, request_code=True, account=None):
            seen["codes"].append(code)
            if code == "bad":
                raise AuthRequired("verification code invalid")
            return {"project_count_visible": 3}

    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    state_path = tmp_path / "state" / "overleaf-sjtu" / "login_state.json"
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    first = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--mfa-method", "email", "--no-remember"],
        env=env,
    )
    bad = runner.invoke(app, ["auth", "login", "--mfa-code", "bad", "--no-remember"], env=env)
    assert state_path.exists()
    retry = runner.invoke(app, ["auth", "login", "--mfa-code", "123456", "--no-remember"], env=env)

    assert first.exit_code == 0
    assert bad.exit_code == 1
    assert "verification code invalid" in str(bad.exception)
    assert retry.exit_code == 0
    assert "Logged in: 3 visible projects" in retry.output
    assert seen == {"requests": 1, "codes": ["bad", "123456"]}
    assert not state_path.exists()


def test_login_with_selected_pending_jaccount_verification_requires_code_or_resend(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<script>account: 'hammer'</script><form><input name='shouldauth' value='true'><input name='c' value='email'><input name='captcha'></form>",
                    "account": "hammer",
                    "method": "email",
                    "methods": ["email"],
                    "cookies": [],
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def begin_jaccount_login(self):
            raise AssertionError("selected pending MFA should not start a new login")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-method", "email", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "waiting for a code" in str(result.exception)
    assert pending.exists()


@pytest.mark.parametrize("method", ["app", "email", "sms"])
def test_login_resends_pending_jaccount_verification(method, monkeypatch, tmp_path) -> None:
    seen = {"requests": []}

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()
            self.session.cookies.set("JAAuthCookie", "ok", domain="jaccount.sjtu.edu.cn", path="/")

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true">'
                b'<input name="c" value="app"><input name="c" value="email"><input name="c" value="sms">'
                b'<input name="captcha"></form>'
            )
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["app", "email", "sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, selected_method):
            seen["requests"].append(selected_method)
            return JAccountVerificationResult(success=True, message=f"sent {selected_method}", retry_seconds=60)

        def complete_jaccount_verification(self, *args, **kwargs):
            raise AssertionError("resend should not submit the verification code")

    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    state_path = tmp_path / "state" / "overleaf-sjtu" / "login_state.json"
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    first = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--mfa-method", method, "--no-remember"],
        env=env,
    )
    resent = runner.invoke(app, ["auth", "login", "--mfa-resend"], env=env)

    assert first.exit_code == 0
    assert resent.exit_code == 0
    assert f"sent {method}" in resent.output
    assert "--mfa-code CODE" in resent.output
    assert seen["requests"] == [method, method]
    assert state_path.exists()


def test_login_keeps_pending_jaccount_verification_after_failed_resend(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<script>account: 'hammer'</script><form><input name='shouldauth' value='true'><input name='c' value='sms'><input name='captcha'></form>",
                    "account": "hammer",
                    "method": "sms",
                    "methods": ["sms"],
                    "cookies": [],
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def request_jaccount_verification(self, response, method):
            return JAccountVerificationResult(success=False, message="Request too often", retry_seconds=60)

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-resend"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "Resend available in 60 seconds" in result.output
    assert "Request too often" in str(result.exception)
    assert pending.exists()


def test_login_mfa_resend_requires_pending_state(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            raise AssertionError("resend should not start a new login")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-resend"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "pending jAccount additional verification state not found or expired" in str(result.exception)


def test_login_mfa_resend_rejects_mismatched_pending_method(monkeypatch, tmp_path) -> None:
    state_dir = tmp_path / "state" / "overleaf-sjtu"
    state_dir.mkdir(parents=True)
    pending = state_dir / "login_state.json"
    pending.write_text(
        __import__("json").dumps(
            {
                "created_at": 4102444800,
                "mfa_state": {
                    "url": "https://jaccount.sjtu.edu.cn/jaccount/jalogin",
                    "html": "<form><input name='shouldauth' value='true'><input name='c' value='email'><input name='captcha'></form>",
                    "method": "email",
                    "methods": ["email"],
                    "cookies": [],
                },
            }
        )
    )

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def request_jaccount_verification(self, response, method):
            raise AssertionError("mismatched method should be rejected before resend")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-method", "sms", "--mfa-resend"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "pending jAccount verification uses email" in str(result.exception)


def test_login_mfa_resend_rejects_code(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-resend", "--mfa-code", "123456"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 2
    assert "--mfa-resend cannot be used with --mfa-code" in result.output


def test_login_rejects_unknown_jaccount_verification_method(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-method", "phone"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 2
    assert "'phone' is not one of 'app', 'email', 'sms'" in result.output


def test_login_rejects_unavailable_jaccount_verification_method(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true">'
                b'<input name="c" value="email"><input name="c" value="sms"><input name="captcha"></form>'
            )
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["email", "sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            raise AssertionError("unavailable method should be rejected before request")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--mfa-method", "app", "--no-remember"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "method app is not available; choose one of: email, sms" in str(result.exception)


def test_login_with_mfa_code_requires_pending_state_or_full_login(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            raise AssertionError("should not start a new login when mfa code has no state")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--mfa-code", "123456"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "pending jAccount additional verification state not found or expired" in str(result.exception)


def test_login_does_not_stage_failed_jaccount_verification_request(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()
            self.session.cookies.set("JAAuthCookie", "ok", domain="jaccount.sjtu.edu.cn", path="/")

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = b'<form><input name="shouldauth" value="true"><input name="c" value="sms"><input name="captcha"></form>'
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            return JAccountVerificationResult(success=False, message="Request too often", retry_seconds=60)

    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login", "--username", "hammer", "--password", "secret", "--mfa-method", "sms", "--no-remember"],
        env=env,
    )

    assert result.exit_code == 1
    assert "Request too often" in str(result.exception)
    assert not (tmp_path / "state" / "overleaf-sjtu" / "login_state.json").exists()


def test_login_with_mfa_code_rejects_mismatched_pending_method(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = requests.Session()
            self.session.cookies.set("JAAuthCookie", "ok", domain="jaccount.sjtu.edu.cn", path="/")

        def begin_jaccount_login(self):
            return {"requires_captcha": False}

        def login_with_jaccount(self, *args, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.url = "https://jaccount.sjtu.edu.cn/jaccount/jalogin"
            response._content = (
                b"<script>account: 'hammer'</script>"
                b'<form><input name="shouldauth" value="true">'
                b'<input name="c" value="email"><input name="c" value="sms"><input name="captcha"></form>'
            )
            challenge = JAccountVerificationChallenge(url=response.url, account="hammer", methods=["email", "sms"])
            raise JAccountVerificationRequired(challenge, response)

        def request_jaccount_verification(self, response, method):
            return JAccountVerificationResult(success=True, message="sent")

    env = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    first = runner.invoke(app, ["auth", "login", "--username", "hammer", "--password", "secret", "--mfa-method", "email"], env=env)
    second = runner.invoke(app, ["auth", "login", "--mfa-method", "sms", "--mfa-code", "123456"], env=env)

    assert first.exit_code == 0
    assert second.exit_code == 1
    assert "pending jAccount verification uses email" in str(second.exception)


def test_project_create_command(monkeypatch, tmp_path) -> None:
    from overleaf_sjtu.models import Project

    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def create_project(self, name):
            return Project(id="0123456789abcdefabcdefab", name=name)

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["project", "create", "--name", "Blank Paper", "--select"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 0
    assert "Created Blank Paper: 0123456789abcdefabcdefab" in result.output
    assert "overleaf project select 0123456789abcdefabcdefab" in result.output


def test_captcha_is_not_saved_by_default_on_tty(tmp_path) -> None:
    assert _save_captcha_if_needed(b"png", None, has_tty=True) is None


def test_captcha_uses_explicit_output_path(tmp_path) -> None:
    output = tmp_path / "captcha.png"

    saved = _save_captcha_if_needed(b"png", output, has_tty=True)

    assert saved == output
    assert output.read_bytes() == b"png"


def test_captcha_uses_temp_file_without_tty() -> None:
    saved = _save_captcha_if_needed(b"png", None, has_tty=False)

    assert saved is not None
    assert saved.name.startswith("overleaf-jaccount-captcha-")
    assert saved.suffix == ".png"
    assert saved.read_bytes() == b"png"
    saved.unlink()


def test_root_file_shortcuts_are_removed() -> None:
    for command in ("ls", "upload", "download", "mkdir"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code != 0
        assert f"No such command '{command}'" in result.output


def test_file_commands_include_cwd_operations() -> None:
    result = runner.invoke(app, ["file"])

    assert result.exit_code == 0
    for command in ("pwd", "ls", "upload", "download", "mkdir", "tree", "cd"):
        assert command in result.output


def test_file_tree_help_has_depth_and_limit() -> None:
    result = runner.invoke(app, ["file", "tree", "--help"])

    assert result.exit_code == 0
    assert "--depth" in result.output
    assert "--limit" in result.output
    assert "--all" in result.output


def test_completion_commands_generate_zsh_script(tmp_path) -> None:
    show = runner.invoke(app, ["completion", "show", "zsh"])

    assert show.exit_code == 0
    assert "#compdef overleaf" in show.output
    assert "_OVERLEAF_COMPLETE=complete_zsh overleaf" in show.output

    completion_dir = tmp_path / "zsh-completions"
    install = runner.invoke(app, ["completion", "install", "zsh", "--path", str(completion_dir), "--no-zshrc"])

    assert install.exit_code == 0
    installed = completion_dir / "_overleaf"
    assert installed.exists()
    assert "#compdef overleaf" in installed.read_text()


def test_completion_commands_generate_bash_script(tmp_path) -> None:
    show = runner.invoke(app, ["completion", "show", "bash"])

    assert show.exit_code == 0
    assert "_overleaf_completion()" in show.output
    assert "_OVERLEAF_COMPLETE=complete_bash" in show.output

    completion_dir = tmp_path / "bash-completions"
    install = runner.invoke(app, ["completion", "install", "bash", "--path", str(completion_dir), "--no-bashrc"])

    assert install.exit_code == 0
    installed = completion_dir / "overleaf"
    assert installed.exists()
    assert "_overleaf_completion()" in installed.read_text()


def test_completion_install_defaults_to_current_shell(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("HOME", str(tmp_path))

    install = runner.invoke(app, ["completion", "install"])

    assert install.exit_code == 0
    assert (tmp_path / ".bash_completion.d" / "overleaf").exists()
    assert ".bashrc" in install.output


def test_completion_command_is_hidden_from_shell_candidates() -> None:
    result = runner.invoke(
        app,
        env={
            "_OVERLEAF_COMPLETE": "complete_zsh",
            "COMP_WORDS": "overleaf ",
            "COMP_CWORD": "1",
        },
    )

    assert result.exit_code == 0
    assert "completion" not in result.output
    for command in ("config", "auth", "project", "compile", "settings", "file"):
        assert command in result.output
