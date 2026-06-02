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
    assert "completion" in root.output
    assert "login" not in [line.split()[0] for line in root.output.splitlines() if line.strip()]
    assert auth.exit_code == 0
    assert "login" in auth.output
    assert "logout" in auth.output
    assert "whoami" in auth.output
    assert "status" in auth.output
    assert "flow" in auth.output
    assert "pending" not in [line.split()[0] for line in auth.output.splitlines() if line.strip()]


def test_login_help_is_interactive_only() -> None:
    result = runner.invoke(app, ["auth", "login", "--help"])

    assert result.exit_code == 0
    assert "--username TEXT" in result.output
    assert "--password TEXT" in result.output
    assert "--cookie TEXT" in result.output
    assert "--captcha" not in result.output
    assert "--mfa-method" not in result.output
    assert "--mfa-code" not in result.output
    assert "--mfa-resend" not in result.output


def test_auth_login_non_interactive_requires_flow(monkeypatch, tmp_path) -> None:
    class FakeClient:
        def __init__(self, config, store, timeout=60):
            self.session = type("Session", (), {"cookies": {}})()

        def begin_jaccount_login(self):
            raise AssertionError("non-interactive auth login should not start jAccount")

    monkeypatch.setattr("overleaf_sjtu.cli.OverleafClient", FakeClient)

    result = runner.invoke(
        app,
        ["auth", "login"],
        env={"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")},
    )

    assert result.exit_code == 1
    assert "non-interactive login requires explicit flow commands" in str(result.exception)


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
    assert show.output.startswith("#compdef overleaf\n")
    assert "#compdef overleaf" in show.output
    assert "autoload -Uz compinit" in show.output
    assert "_overleaf()" in show.output
    assert "_OVERLEAF_COMPLETE=complete_zsh overleaf" in show.output

    completion_dir = tmp_path / "zsh-completions"
    install = runner.invoke(app, ["completion", "install", "zsh", "--path", str(completion_dir), "--no-zshrc"])

    assert install.exit_code == 0
    installed = completion_dir / "_overleaf"
    assert installed.exists()
    installed_text = installed.read_text()
    assert installed_text.startswith("#compdef overleaf\n")
    assert "autoload -Uz compinit" in installed_text
    assert "_overleaf()" in installed_text


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


def test_completion_command_is_visible_in_shell_candidates() -> None:
    result = runner.invoke(
        app,
        env={
            "_OVERLEAF_COMPLETE": "complete_zsh",
            "COMP_WORDS": "overleaf ",
            "COMP_CWORD": "1",
        },
    )

    assert result.exit_code == 0
    for command in ("config", "auth", "project", "compile", "settings", "file", "completion"):
        assert command in result.output


def test_legacy_completion_instruction_still_works() -> None:
    result = runner.invoke(
        app,
        env={
            "_OVERLEAF_COMPLETE": "zsh_complete",
            "COMP_WORDS": "overleaf ",
            "COMP_CWORD": "1",
        },
    )

    assert result.exit_code == 0
    assert "Shell complete not supported" not in result.output
    for command in ("config", "auth", "project", "compile", "settings", "file", "completion"):
        assert command in result.output
