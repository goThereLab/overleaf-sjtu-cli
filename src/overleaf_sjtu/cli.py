from __future__ import annotations

import getpass
import atexit
import contextlib
import hashlib
import io
import json
import os
import posixpath
import shlex
import subprocess
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import click
import requests
import typer.main
import typer
from typer.core import TyperGroup

try:
    from typer._completion_classes import BashComplete, ZshComplete, completion_init
except ImportError:  # pragma: no cover - compatibility with older Typer internals
    from click.shell_completion import BashComplete, ZshComplete

    def completion_init() -> None:
        return None

completion_init()

from . import __version__
from .auth import cookie_header_to_jar, has_any_cookie, save_requests_cookies
from .captcha import captcha_to_ansi_blocks
from .client import AuthRequired, JAccountVerificationRequired, OverleafClient, OverleafError
from .config import ConfigStore
from .credentials import delete_saved_credentials, get_saved_credentials, save_credentials
from .output import console, emit_file_entries, emit_json, emit_projects, error


class MfaMethod(str, Enum):
    app = "app"
    email = "email"
    sms = "sms"


class Compiler(str, Enum):
    latex = "latex"
    lualatex = "lualatex"
    pdflatex = "pdflatex"
    xelatex = "xelatex"


class CompactHelpGroup(TyperGroup):
    def _hide_compat_commands(self) -> None:
        return None

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        self._hide_compat_commands()
        super().format_commands(ctx, formatter)

    def get_help(self, ctx: click.Context) -> str:
        self._hide_compat_commands()
        return super().get_help(ctx)

    def shell_complete(self, ctx: click.Context, incomplete: str) -> list[click.shell_completion.CompletionItem]:
        self._hide_compat_commands()
        return super().shell_complete(ctx, incomplete)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            click.echo(ctx.get_help(), color=ctx.color)
            ctx.exit(0)
        return super().parse_args(ctx, args)


_TYPER_KWARGS = {
    "cls": CompactHelpGroup,
    "no_args_is_help": True,
    "add_completion": False,
    "rich_markup_mode": None,
    "pretty_exceptions_enable": False,
}

app = typer.Typer(help="SJTU Overleaf command line client.", **_TYPER_KWARGS)
auth_app = typer.Typer(help="Authentication commands.", **_TYPER_KWARGS)
auth_flow_app = typer.Typer(help="Explicit multi-step login flow commands for agents and scripts.", **_TYPER_KWARGS)
project_app = typer.Typer(help="Manage projects.", **_TYPER_KWARGS)
compile_app = typer.Typer(help="Compile LaTeX projects and fetch outputs.", **_TYPER_KWARGS)
settings_app = typer.Typer(help="Change project settings.", **_TYPER_KWARGS)
completion_app = typer.Typer(help="Shell completion commands.", **_TYPER_KWARGS)
file_app = typer.Typer(
    help="File-level operations inside a project.",
    cls=CompactHelpGroup,
    invoke_without_command=True,
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
app.add_typer(auth_app, name="auth")
auth_app.add_typer(auth_flow_app, name="flow")
app.add_typer(project_app, name="project")
app.add_typer(compile_app, name="compile")
app.add_typer(settings_app, name="settings")
app.add_typer(completion_app, name="completion")
app.add_typer(file_app, name="file")


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", callback=version_callback, help="Show version and exit."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Override Overleaf base URL."),
    project_url: Optional[str] = typer.Option(None, "--project-url", help="Override project listing URL."),
    timeout: int = typer.Option(60, "--timeout", min=5, help="HTTP timeout in seconds."),
) -> None:
    store = ConfigStore()
    config = store.load()
    if base_url:
        config.base_url = base_url.rstrip("/")
    if project_url:
        config.project_url = project_url
    elif base_url:
        config.project_url = f"{base_url.rstrip('/')}/project"
    ctx.obj = {"store": store, "config": config, "client": OverleafClient(config, store, timeout=timeout)}


def get_client(ctx: typer.Context) -> OverleafClient:
    return ctx.obj["client"]


def get_store(ctx: typer.Context) -> ConfigStore:
    return ctx.obj["store"]


@auth_app.command()
def login(
    ctx: typer.Context,
    username: Optional[str] = typer.Option(None, "--username", "-u", envvar="OVERLEAF_USERNAME", help="jAccount username."),
    password: Optional[str] = typer.Option(None, "--password", envvar="OVERLEAF_PASSWORD", help="jAccount password."),
    cookie: Optional[str] = typer.Option(None, "--cookie", help="Import a raw Cookie header."),
    remember: Optional[bool] = typer.Option(
        None,
        "--remember/--no-remember",
        help="Save jAccount credentials to the shared Canvas keyring entry. Defaults to prompting when new credentials are entered.",
    ),
    save_base_url: bool = typer.Option(True, "--save-base-url/--no-save-base-url", help="Persist URL options."),
) -> None:
    """Log in through jAccount over HTTP and save the authenticated session."""
    client = get_client(ctx)
    store = get_store(ctx)
    config = ctx.obj["config"]
    if cookie:
        domain = urlparse(config.base_url).hostname or "latex.sjtu.edu.cn"
        jar = cookie_header_to_jar(cookie, domain)
        save_requests_cookies(store.cookie_path, jar)
        client.session.cookies.update(jar)
    else:
        has_tty = sys.stdin.isatty() and sys.stdout.isatty()
        if not has_tty:
            raise AuthRequired(
                "non-interactive login requires explicit flow commands; "
                "run `overleaf auth flow start --captcha-output captcha.png`"
            )
        login_state = client.begin_jaccount_login()
        captcha = None
        if login_state["requires_captcha"]:
            captcha_bytes = client.get_login_captcha(login_state)
            _display_login_captcha(captcha_bytes)
            captcha = typer.prompt("Captcha")
        saved_username, saved_password = get_saved_credentials()
        used_saved_password = False
        entered_password = False
        if not username:
            username = saved_username or typer.prompt("jAccount")
        if not password:
            if username == saved_username and saved_password:
                password = saved_password
                used_saved_password = True
            else:
                password = getpass.getpass("Password: ")
                entered_password = True
        try:
            info = client.login_with_jaccount(username=username, password=password, captcha=captcha, login_state=login_state)
        except JAccountVerificationRequired as exc:
            mfa_method = _prompt_mfa_method(exc.challenge.methods)
            sent = client.request_jaccount_verification(exc.response, mfa_method)
            typer.echo(sent.message)
            if sent.retry_seconds:
                typer.echo(f"Resend available in {sent.retry_seconds} seconds")
            if not sent.success:
                raise AuthRequired(sent.message) from exc
            mfa_code = typer.prompt("Verification code")
            info = client.complete_jaccount_verification(exc.response, mfa_method, mfa_code, request_code=False)
        store.clear_login_state()
        should_save = remember
        if should_save is None:
            should_save = _prompt_remember_credentials() if entered_password and not used_saved_password else False
        if should_save:
            try:
                save_credentials(username, password)
            except Exception as exc:
                error(f"Warning: login succeeded, but keyring save failed: {exc}")
        if save_base_url:
            store.save(config)
        console.print(f"Logged in: {info['project_count_visible']} visible projects")
        return
    if save_base_url:
        store.save(config)
    info = client.whoami()
    console.print(f"Logged in: {info['project_count_visible']} visible projects")


def _save_captcha_if_needed(captcha_bytes: bytes, output: Optional[Path], has_tty: bool) -> Optional[Path]:
    if output is None and has_tty:
        return None
    path = output
    if path is None:
        with tempfile.NamedTemporaryFile(prefix="overleaf-jaccount-captcha-", suffix=".png", delete=False) as fh:
            path = Path(fh.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(captcha_bytes)
    return path


def _display_login_captcha(captcha_bytes: bytes, *, windows: bool | None = None) -> None:
    use_windows_fallback = os.name == "nt" if windows is None else windows
    if use_windows_fallback:
        saved_captcha = _save_captcha_if_needed(captcha_bytes, None, has_tty=False)
        if saved_captcha:
            typer.echo(f"Captcha saved: {saved_captcha}")
    try:
        _, _, rows = captcha_to_ansi_blocks(captcha_bytes, windows=use_windows_fallback)
        for row in rows:
            typer.echo(row, color=not use_windows_fallback)
    except Exception as exc:
        error(f"Warning: could not render captcha in terminal: {exc}")


def _save_pending_login_state(
    store: ConfigStore,
    login_state: dict,
    captcha_path: Optional[Path],
    mfa_method: str | None = None,
    flow_path: Optional[Path] = None,
) -> None:
    payload = {
        "created_at": int(time.time()),
        "captcha_path": str(captcha_path) if captcha_path else None,
        "login_state": login_state,
    }
    if mfa_method:
        payload["mfa_method"] = mfa_method
    _write_flow_payload(flow_path or store.login_state_path, payload)


def _save_pending_mfa_state(
    store: ConfigStore,
    response: requests.Response,
    challenge,
    method: str | None,
    client: OverleafClient,
    flow_path: Optional[Path] = None,
) -> None:
    payload = {
        "created_at": int(time.time()),
        "mfa_state": {
            "url": response.url,
            "html": response.text,
            "account": challenge.account,
            "methods": challenge.methods,
            "cookies": _cookie_list(client.session.cookies),
        },
    }
    if method:
        payload["mfa_state"]["method"] = method
    _write_flow_payload(flow_path or store.login_state_path, payload)


def _refresh_pending_mfa_state(store: ConfigStore, state: dict, client: OverleafClient, flow_path: Optional[Path] = None) -> None:
    refreshed = dict(state)
    refreshed["cookies"] = _cookie_list(client.session.cookies)
    payload = {"created_at": int(time.time()), "mfa_state": refreshed}
    _write_flow_payload(flow_path or store.login_state_path, payload)


def _write_flow_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def _flow_path(store: ConfigStore, flow: Optional[Path]) -> Path:
    return flow or store.login_flow_path


def _clear_flow_path(path: Path) -> None:
    if path.exists():
        path.unlink()


def _load_pending_mfa_state(store: ConfigStore, flow_path: Optional[Path] = None) -> Optional[dict]:
    payload = _load_pending_payload(store, flow_path)
    if payload is None:
        return None
    if int(time.time()) - int(payload.get("created_at") or 0) > 600:
        _clear_flow_path(flow_path or store.login_state_path)
        return None
    return payload.get("mfa_state")


def _response_from_pending_mfa(state: dict) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = state["url"]
    response._content = str(state.get("html") or "").encode("utf-8")
    response.encoding = "utf-8"
    return response


def _cookie_list(cookies) -> list[dict]:
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
    return data


def _restore_cookie_list(client: OverleafClient, data: list[dict]) -> None:
    for item in data:
        client.session.cookies.set(
            item["name"],
            item["value"],
            domain=item.get("domain"),
            path=item.get("path") or "/",
            secure=bool(item.get("secure")),
            expires=item.get("expires"),
        )


def _prompt_mfa_method(methods: list[str]) -> str:
    labels = {"app": "My SJTU app", "email": "email", "sms": "SMS"}
    available = [method for method in ("app", "email", "sms") if method in methods]
    if not available:
        raise AuthRequired("jAccount additional verification is required, but no supported method was found")
    typer.echo("jAccount additional verification required:")
    for index, method in enumerate(available, start=1):
        typer.echo(f"  {index}. {method} ({labels[method]})")
    while True:
        answer = typer.prompt("Verification method", default=available[0]).strip().lower()
        if answer in available:
            return answer
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(available):
                return available[index - 1]
        typer.echo(f"Choose one of: {', '.join(available)}")


def _load_pending_login_state(store: ConfigStore, flow_path: Optional[Path] = None) -> Optional[dict]:
    payload = _load_pending_login_payload(store, flow_path)
    if payload is None:
        return None
    state = payload.get("login_state")
    return state if isinstance(state, dict) else None


def _load_pending_login_payload(store: ConfigStore, flow_path: Optional[Path] = None) -> Optional[dict]:
    payload = _load_pending_payload(store, flow_path)
    if payload is None:
        return None
    if int(time.time()) - int(payload.get("created_at") or 0) > 600:
        _clear_flow_path(flow_path or store.login_state_path)
        return None
    return payload if isinstance(payload.get("login_state"), dict) else None


def _load_pending_payload(store: ConfigStore, flow_path: Optional[Path] = None) -> Optional[dict]:
    path = flow_path or store.login_state_path
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _pending_status(store: ConfigStore, flow_path: Optional[Path] = None) -> dict:
    path = flow_path or (store.login_flow_path if store.login_flow_path.exists() else store.login_state_path)
    payload = _load_pending_payload(store, path)
    if payload is None:
        return {"pending": False, "flow": str(path)}
    created_at = int(payload.get("created_at") or 0)
    age_seconds = max(0, int(time.time()) - created_at)
    remaining_seconds = 600 - age_seconds
    if remaining_seconds <= 0:
        _clear_flow_path(path)
        return {"pending": False, "expired": True, "flow": str(path)}
    mfa_state = payload.get("mfa_state")
    if isinstance(mfa_state, dict):
        return {
            "pending": True,
            "flow": str(path),
            "type": "mfa",
            "method": mfa_state.get("method"),
            "methods": mfa_state.get("methods") or [],
            "account": mfa_state.get("account"),
            "age_seconds": age_seconds,
            "remaining_seconds": remaining_seconds,
        }
    login_state = payload.get("login_state")
    if isinstance(login_state, dict):
        return {
            "pending": True,
            "flow": str(path),
            "type": "captcha",
            "captcha_path": payload.get("captcha_path"),
            "mfa_method": payload.get("mfa_method"),
            "age_seconds": age_seconds,
            "remaining_seconds": remaining_seconds,
        }
    return {"pending": False}


def _flow_submit_password_hint(path: Path, needs_captcha: bool = True) -> str:
    command = f"overleaf auth flow submit-password --flow {shlex.quote(str(path))} --username USERNAME --password PASSWORD"
    if needs_captcha:
        command += " --captcha CAPTCHA"
    return command


def _print_pending_flow_status(status: dict, flow_arg: Path) -> None:
    if status.get("type") == "mfa":
        methods = ", ".join(status.get("methods") or [])
        method = status.get("method")
        typer.echo(f"Pending jAccount additional verification: method={method or 'not selected'}")
        typer.echo(f"flow: {flow_arg}")
        if methods:
            typer.echo(f"available methods: {methods}")
        if status.get("account"):
            typer.echo(f"account: {status['account']}")
        typer.echo(f"expires in: {status['remaining_seconds']} seconds")
        typer.echo("Next:")
        if method:
            typer.echo(f"  overleaf auth flow mfa-submit --flow {shlex.quote(str(flow_arg))} --code CODE")
            typer.echo(f"  overleaf auth flow resend --flow {shlex.quote(str(flow_arg))}")
        else:
            typer.echo(f"  overleaf auth flow mfa-request --flow {shlex.quote(str(flow_arg))} --method METHOD")
        return
    if status.get("type") == "captcha":
        typer.echo("Pending jAccount password flow")
        typer.echo(f"flow: {flow_arg}")
        if status.get("captcha_path"):
            typer.echo(f"captcha: {status['captcha_path']}")
        typer.echo(f"expires in: {status['remaining_seconds']} seconds")
        typer.echo("Next:")
        typer.echo(f"  {_flow_submit_password_hint(flow_arg, needs_captcha=bool(status.get('captcha_path')))}")


def _prompt_remember_credentials() -> bool:
    if not sys.stdin.isatty():
        return False
    while True:
        answer = typer.prompt("Remember jAccount credentials in shared Canvas keyring? [y/n]").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        error("Please answer y or n")


@auth_app.command()
def logout(
    ctx: typer.Context,
    forget_credentials: bool = typer.Option(
        False,
        "--forget-credentials",
        help="Also delete the shared Canvas jAccount credentials from keyring.",
    ),
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Specific jAccount username to forget."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt before deleting keyring credentials."),
) -> None:
    """Remove the saved session cookies."""
    get_store(ctx).clear_cookies()
    get_store(ctx).clear_login_state()
    if not forget_credentials:
        console.print("Logged out")
        return
    if not yes:
        message = "Forget shared Canvas jAccount credentials from keyring?"
        if not sys.stdin.isatty():
            raise typer.BadParameter("pass --yes with --forget-credentials when stdin is not a TTY")
        typer.confirm(message, abort=True)
    deleted = delete_saved_credentials(username)
    console.print(f"Logged out; deleted {len(deleted)} keyring entries")


@auth_app.command()
def whoami(ctx: typer.Context, json_: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Check whether the saved session is authenticated."""
    info = get_client(ctx).whoami()
    if json_:
        emit_json(info)
    else:
        console.print(f"Authenticated at {info['base_url']} ({info['project_count_visible']} visible projects)")


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    check: bool = typer.Option(False, "--check", help="Verify the saved session against the server."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show saved session and pending login-flow state."""
    store = get_store(ctx)
    client = get_client(ctx)
    pending = _pending_status(store)
    data = {
        "session_present": has_any_cookie(client.session.cookies),
        "session_valid": None,
        "authenticated": None,
        "cookie_path": str(store.cookie_path),
        "flow_path": str(store.login_flow_path),
        "pending": bool(pending.get("pending")),
        "pending_type": pending.get("type"),
        "pending_flow": pending.get("flow"),
    }
    if check:
        try:
            info = client.whoami()
        except (AuthRequired, OverleafError):
            data["session_valid"] = False
            data["authenticated"] = False
        else:
            data.update(
                {
                    "session_valid": True,
                    "authenticated": True,
                    "base_url": info["base_url"],
                    "project_count_visible": info["project_count_visible"],
                }
            )
    if json_:
        emit_json(data)
        return
    console.print(f"session: {'present' if data['session_present'] else 'missing'}")
    if check:
        console.print(f"session_valid: {data['session_valid']}")
        if data.get("project_count_visible") is not None:
            console.print(f"projects: {data['project_count_visible']}")
    console.print(f"cookie_file: {data['cookie_path']}")
    if data["pending"]:
        console.print(f"pending_flow: {data['pending_type']} ({data['pending_flow']})")
    else:
        console.print("pending_flow: none")
    console.print("Next:")
    if not check:
        console.print("  overleaf auth status --check")
    if not data["session_present"] or data.get("session_valid") is False:
        console.print("  overleaf auth login")



@auth_flow_app.command("status")
def auth_flow_status(
    ctx: typer.Context,
    flow: Optional[Path] = typer.Option(None, "--flow", help="Explicit login flow state file."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show an explicit login flow state."""
    path = _flow_path(get_store(ctx), flow)
    status = _pending_status(get_store(ctx), path)
    if json_:
        emit_json(status)
        return
    if not status.get("pending"):
        typer.echo(f"No pending jAccount login flow: {path}")
        return
    _print_pending_flow_status(status, flow_arg=path)


@auth_flow_app.command("start")
def auth_flow_start(
    ctx: typer.Context,
    flow: Optional[Path] = typer.Option(None, "--flow", help="Explicit login flow state file."),
    captcha_output: Optional[Path] = typer.Option(None, "--captcha-output", help="Save captcha image to this path."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Start a jAccount login flow and save its state."""
    store = get_store(ctx)
    client = get_client(ctx)
    path = _flow_path(store, flow)
    login_state = client.begin_jaccount_login()
    captcha_path = None
    if login_state["requires_captcha"]:
        captcha_path = _save_captcha_if_needed(client.get_login_captcha(login_state), captcha_output, has_tty=False)
    _save_pending_login_state(store, login_state, captcha_path, flow_path=path)
    state = "captcha_required" if captcha_path else "password_required"
    data = {
        "flow": str(path),
        "state": state,
        "captcha_path": str(captcha_path) if captcha_path else None,
        "next": [_flow_submit_password_hint(path, needs_captcha=bool(captcha_path))],
    }
    if json_:
        emit_json(data)
        return
    typer.echo(f"Started jAccount login flow: {path}")
    if captcha_path:
        typer.echo(f"Captcha saved: {captcha_path}")
        typer.echo("Next:")
        typer.echo("  read the captcha image")
    else:
        typer.echo("Next:")
    typer.echo(f"  {data['next'][0]}")


@auth_flow_app.command("submit-password")
def auth_flow_submit_password(
    ctx: typer.Context,
    username: str = typer.Option(..., "--username", "-u", envvar="OVERLEAF_USERNAME", help="jAccount username."),
    password: str = typer.Option(..., "--password", envvar="OVERLEAF_PASSWORD", help="jAccount password."),
    captcha: Optional[str] = typer.Option(None, "--captcha", help="jAccount captcha code."),
    flow: Optional[Path] = typer.Option(None, "--flow", help="Explicit login flow state file."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Submit username, password, and optional CAPTCHA for a saved login flow."""
    store = get_store(ctx)
    client = get_client(ctx)
    path = _flow_path(store, flow)
    payload = _load_pending_login_payload(store, path)
    if payload is None:
        raise AuthRequired(f"pending jAccount password flow not found or expired: {path}")
    if payload.get("captcha_path") and not captcha:
        raise AuthRequired(f"captcha is required; read {payload['captcha_path']} and pass --captcha CAPTCHA")
    try:
        info = client.login_with_jaccount(username=username, password=password, captcha=captcha, login_state=payload["login_state"])
    except JAccountVerificationRequired as exc:
        _save_pending_mfa_state(store, exc.response, exc.challenge, None, client, flow_path=path)
        data = {
            "flow": str(path),
            "state": "mfa_required",
            "methods": exc.challenge.methods,
            "account": exc.challenge.account,
            "next": [f"overleaf auth flow mfa-request --flow {shlex.quote(str(path))} --method METHOD"],
        }
        if json_:
            emit_json(data)
            return
        typer.echo("jAccount additional verification required")
        typer.echo(f"available methods: {', '.join(exc.challenge.methods)}")
        typer.echo("Next:")
        typer.echo(f"  {data['next'][0]}")
        return
    _clear_flow_path(path)
    data = {"flow": str(path), "state": "authenticated", "project_count_visible": info["project_count_visible"]}
    if json_:
        emit_json(data)
    else:
        console.print(f"Logged in: {info['project_count_visible']} visible projects")


@auth_flow_app.command("mfa-request")
def auth_flow_mfa_request(
    ctx: typer.Context,
    method: MfaMethod = typer.Option(..., "--method", help="Verification method: app, email, or sms."),
    flow: Optional[Path] = typer.Option(None, "--flow", help="Explicit login flow state file."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Request an MFA code for a saved login flow."""
    store = get_store(ctx)
    client = get_client(ctx)
    path = _flow_path(store, flow)
    state = _load_pending_mfa_state(store, path)
    if state is None:
        raise AuthRequired(f"pending jAccount MFA flow not found or expired: {path}")
    method_value = method.value
    methods = state.get("methods") or []
    if method_value not in methods:
        raise AuthRequired(f"jAccount verification method {method_value} is not available; choose one of: {', '.join(methods)}")
    _restore_cookie_list(client, state.get("cookies") or [])
    response = _response_from_pending_mfa(state)
    sent = client.request_jaccount_verification(response, method_value)
    selected = dict(state)
    selected["method"] = method_value
    _refresh_pending_mfa_state(store, selected, client, flow_path=path)
    data = {
        "flow": str(path),
        "state": "mfa_code_requested" if sent.success else "mfa_request_failed",
        "method": method_value,
        "message": sent.message,
        "retry_seconds": sent.retry_seconds,
        "next": [f"overleaf auth flow mfa-submit --flow {shlex.quote(str(path))} --code CODE"],
    }
    if json_:
        emit_json(data)
        return
    typer.echo(sent.message)
    if sent.retry_seconds:
        typer.echo(f"Resend available in {sent.retry_seconds} seconds")
    if not sent.success:
        raise AuthRequired(sent.message)
    typer.echo("Next:")
    typer.echo(f"  {data['next'][0]}")


@auth_flow_app.command("mfa-submit")
def auth_flow_mfa_submit(
    ctx: typer.Context,
    code: str = typer.Option(..., "--code", help="Verification code."),
    flow: Optional[Path] = typer.Option(None, "--flow", help="Explicit login flow state file."),
    trust_mfa: bool = typer.Option(True, "--trust-mfa/--no-trust-mfa", help="Trust this device when submitting additional verification."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Submit an MFA code and finish a saved login flow."""
    store = get_store(ctx)
    client = get_client(ctx)
    path = _flow_path(store, flow)
    state = _load_pending_mfa_state(store, path)
    if state is None:
        raise AuthRequired(f"pending jAccount MFA flow not found or expired: {path}")
    method = state.get("method")
    if not method:
        raise AuthRequired(f"pending jAccount MFA flow has no selected method; run `overleaf auth flow mfa-request --flow {path} --method METHOD`")
    _restore_cookie_list(client, state.get("cookies") or [])
    info = client.complete_jaccount_verification(
        _response_from_pending_mfa(state),
        method,
        code,
        trust=trust_mfa,
        request_code=False,
        account=state.get("account"),
    )
    _clear_flow_path(path)
    data = {"flow": str(path), "state": "authenticated", "project_count_visible": info["project_count_visible"]}
    if json_:
        emit_json(data)
    else:
        console.print(f"Logged in: {info['project_count_visible']} visible projects")


@auth_flow_app.command("resend")
def auth_flow_resend(
    ctx: typer.Context,
    flow: Optional[Path] = typer.Option(None, "--flow", help="Explicit login flow state file."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Resend the MFA code for a saved login flow."""
    store = get_store(ctx)
    client = get_client(ctx)
    path = _flow_path(store, flow)
    state = _load_pending_mfa_state(store, path)
    if state is None:
        raise AuthRequired(f"pending jAccount MFA flow not found or expired: {path}")
    method = state.get("method")
    if not method:
        raise AuthRequired(f"pending jAccount MFA flow has no selected method; run `overleaf auth flow mfa-request --flow {path} --method METHOD`")
    _restore_cookie_list(client, state.get("cookies") or [])
    sent = client.request_jaccount_verification(_response_from_pending_mfa(state), method)
    _refresh_pending_mfa_state(store, state, client, flow_path=path)
    data = {
        "flow": str(path),
        "state": "mfa_code_requested" if sent.success else "mfa_request_failed",
        "method": method,
        "message": sent.message,
        "retry_seconds": sent.retry_seconds,
        "next": [f"overleaf auth flow mfa-submit --flow {shlex.quote(str(path))} --code CODE"],
    }
    if json_:
        emit_json(data)
        return
    typer.echo(sent.message)
    if sent.retry_seconds:
        typer.echo(f"Resend available in {sent.retry_seconds} seconds")
    if not sent.success:
        raise AuthRequired(sent.message)
    typer.echo("Next:")
    typer.echo(f"  {data['next'][0]}")


@auth_flow_app.command("cancel")
def auth_flow_cancel(
    ctx: typer.Context,
    flow: Optional[Path] = typer.Option(None, "--flow", help="Explicit login flow state file."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Cancel and remove a saved login flow."""
    path = _flow_path(get_store(ctx), flow)
    existed = path.exists()
    _clear_flow_path(path)
    data = {"flow": str(path), "cancelled": existed}
    if json_:
        emit_json(data)
    else:
        typer.echo(f"Cancelled login flow: {path}" if existed else f"No login flow found: {path}")


@app.command("config")
def show_config(ctx: typer.Context, json_: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Show CLI configuration paths and active project."""
    store = get_store(ctx)
    config = ctx.obj["config"]
    data = {
        "base_url": config.base_url,
        "project_url": config.project_url,
        "current_project": config.current_project,
        "config_path": str(store.path),
        "cookie_path": str(store.cookie_path),
        "has_cookies": has_any_cookie(get_client(ctx).session.cookies),
    }
    if json_:
        emit_json(data)
    else:
        for key, value in data.items():
            console.print(f"{key}: {value}")


def _zsh_completion_script() -> str:
    root_command = typer.main.get_command(app)
    # Typer keeps the pre-Click-8 completion instruction order internally
    # (`complete_zsh`), while Click's zsh template emits `zsh_complete`.
    script = ZshComplete(root_command, {}, "overleaf", "_OVERLEAF_COMPLETE").source().replace(
        "_OVERLEAF_COMPLETE=zsh_complete",
        "_OVERLEAF_COMPLETE=complete_zsh",
    ).lstrip()
    script = script.replace("_overleaf_completion()", "_overleaf()", 1).replace(
        "compdef _overleaf_completion overleaf",
        "compdef _overleaf overleaf",
    )
    return script.replace(
        "#compdef overleaf\n\n",
        "#compdef overleaf\n\n"
        "autoload -Uz compinit\n"
        "if ! whence compdef >/dev/null 2>&1; then\n"
        "  compinit\n"
        "fi\n\n",
        1,
    )


def _bash_completion_script() -> str:
    root_command = typer.main.get_command(app)
    with contextlib.redirect_stderr(io.StringIO()):
        return BashComplete(root_command, {}, "overleaf", "_OVERLEAF_COMPLETE").source()


def _default_completion_shell() -> str:
    shell = Path(os.environ.get("SHELL", "")).name
    if shell in {"zsh", "bash"}:
        return shell
    return "zsh"


def _format_home_path(path: Path) -> str:
    home = Path.home()
    try:
        rel = path.resolve().relative_to(home.resolve())
    except ValueError:
        return str(path)
    if str(rel) == ".":
        return "$HOME"
    return f"$HOME/{rel.as_posix()}"


def _ensure_zshrc_completion(completion_dir: Path) -> bool:
    zshrc = Path.home() / ".zshrc"
    completion_dir_text = _format_home_path(completion_dir)
    block = (
        "\n# overleaf completion\n"
        f"fpath=({completion_dir_text} $fpath)\n"
        "autoload -Uz compinit\n"
        "compinit\n"
    )
    existing = zshrc.read_text() if zshrc.exists() else ""
    if str(completion_dir) in existing or completion_dir_text in existing:
        return False
    with zshrc.open("a") as fp:
        if existing and not existing.endswith("\n"):
            fp.write("\n")
        fp.write(block)
    return True


def _ensure_bashrc_completion(completion_file: Path) -> bool:
    bashrc = Path.home() / ".bashrc"
    completion_file_text = _format_home_path(completion_file)
    block = (
        "\n# overleaf completion\n"
        f'[ -f "{completion_file_text}" ] && source "{completion_file_text}"\n'
    )
    existing = bashrc.read_text() if bashrc.exists() else ""
    if str(completion_file) in existing or completion_file_text in existing:
        return False
    with bashrc.open("a") as fp:
        if existing and not existing.endswith("\n"):
            fp.write("\n")
        fp.write(block)
    return True


@completion_app.command("show")
def completion_show(
    shell: Optional[str] = typer.Argument(None, help="Shell name: zsh or bash. Defaults to the current shell."),
) -> None:
    """Print shell completion script."""
    shell = shell or _default_completion_shell()
    if shell == "zsh":
        typer.echo(_zsh_completion_script())
    elif shell == "bash":
        typer.echo(_bash_completion_script())
    else:
        raise typer.BadParameter("supported shells: zsh, bash")


@completion_app.command("install")
def completion_install(
    shell: Optional[str] = typer.Argument(None, help="Shell name: zsh or bash. Defaults to the current shell."),
    path: Optional[Path] = typer.Option(None, "--path", "-p", help="Completion directory."),
    zshrc: bool = typer.Option(True, "--zshrc/--no-zshrc", help="Add the completion directory to ~/.zshrc."),
    bashrc: bool = typer.Option(True, "--bashrc/--no-bashrc", help="Source the completion script from ~/.bashrc."),
) -> None:
    """Install shell completion."""
    shell = shell or _default_completion_shell()
    if shell == "zsh":
        completion_dir = path or (Path.home() / ".zsh" / "completions")
        completion_dir.mkdir(parents=True, exist_ok=True)
        target = completion_dir / "_overleaf"
        target.write_text(_zsh_completion_script())
        changed_zshrc = _ensure_zshrc_completion(completion_dir) if zshrc else False
        typer.echo(f"Installed {target}")
        if changed_zshrc:
            typer.echo("Updated ~/.zshrc")
        elif zshrc:
            typer.echo("~/.zshrc already includes this completion directory")
        typer.echo("Restart zsh or run: exec zsh")
    elif shell == "bash":
        completion_dir = path or (Path.home() / ".bash_completion.d")
        completion_dir.mkdir(parents=True, exist_ok=True)
        target = completion_dir / "overleaf"
        target.write_text(_bash_completion_script())
        changed_bashrc = _ensure_bashrc_completion(target) if bashrc else False
        typer.echo(f"Installed {target}")
        if changed_bashrc:
            typer.echo("Updated ~/.bashrc")
        elif bashrc:
            typer.echo("~/.bashrc already sources this completion script")
        typer.echo("Restart bash or run: exec bash")
    else:
        raise typer.BadParameter("supported shells: zsh, bash")


@project_app.command("list")
def project_list(
    ctx: typer.Context,
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum projects to show."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only project IDs."),
) -> None:
    """List visible projects."""
    projects = get_client(ctx).list_projects()[:limit]
    if json_:
        emit_json(projects)
    else:
        emit_projects(projects, quiet=quiet)


@project_app.command("show")
def project_show(ctx: typer.Context, project: str, json_: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Show project details."""
    data = get_client(ctx).show_project(project)
    if json_:
        emit_json(data)
    else:
        console.print(f"{data['id']}: {data['url']}")


@project_app.command("create")
def project_create(
    ctx: typer.Context,
    name: str = typer.Option(..., "--name", "-n", help="Project name."),
    select: bool = typer.Option(False, "--select", help="Make the created project current."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Create a blank project."""
    project = get_client(ctx).create_project(name)
    if select:
        config = ctx.obj["config"]
        config.current_project = project.id
        get_store(ctx).save(config)
    if json_:
        emit_json(project)
    else:
        console.print(f"Created {project.name}: {project.id}")
        console.print("Next:")
        console.print(f"  overleaf project select {project.id}")
        console.print(f"  overleaf file upload main.tex /main.tex --project {project.id}")


@project_app.command("upload")
def project_upload(
    ctx: typer.Context,
    archive: Path = typer.Argument(..., exists=True, readable=True, help="Zip archive to upload."),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Project name."),
    select: bool = typer.Option(False, "--select", help="Make the uploaded project current."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Upload a zip as a project."""
    project = get_client(ctx).upload_project(archive, name=name)
    if select:
        config = ctx.obj["config"]
        config.current_project = project.id
        get_store(ctx).save(config)
    if json_:
        emit_json(project)
    else:
        console.print(f"Uploaded {project.name}: {project.id}")


@project_app.command("download")
def project_download(
    ctx: typer.Context,
    project: Optional[str] = typer.Argument(None, help="Project ID or URL. Defaults to current project."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination zip path."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only output path."),
) -> None:
    """Download project source zip."""
    pid = get_client(ctx).resolve_project(project)
    path = get_client(ctx).download_project_zip(pid, output)
    typer.echo(str(path) if quiet else f"Saved {path}")


@project_app.command("delete")
def project_delete(
    ctx: typer.Context,
    project: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete a project."""
    pid = get_client(ctx).resolve_project(project)
    if not yes:
        typer.confirm(f"Delete project {pid} from {ctx.obj['config'].base_url}?", abort=True)
    get_client(ctx).delete_project(pid)
    config = ctx.obj["config"]
    if config.current_project == pid:
        config.current_project = None
        get_store(ctx).save(config)
    console.print(f"Deleted {pid}")


@project_app.command("select")
def project_select(ctx: typer.Context, project: str) -> None:
    """Set the current project."""
    pid = get_client(ctx).resolve_project(project)
    config = ctx.obj["config"]
    config.current_project = pid
    config.defaults.setdefault("file_cwd", {}).setdefault(pid, "/")
    get_store(ctx).save(config)
    console.print(f"Current project: {pid}")


@project_app.command("current")
def project_current(
    ctx: typer.Context,
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the project ID."),
) -> None:
    """Show the current project."""
    pid = ctx.obj["config"].current_project
    if not pid:
        raise typer.BadParameter("no current project; run `overleaf project select <id>`")
    if quiet:
        console.print(pid)
        return

    project = next((item for item in get_client(ctx).list_projects() if item.id == pid), None)
    if json_:
        emit_json(
            {
                "current_project": pid,
                "project": project,
            }
        )
    elif project:
        console.print(f"{project.id}\t{project.name}")
    else:
        console.print(f"{pid}\t<not found in visible projects>")


@project_app.command("clear")
def project_clear(ctx: typer.Context) -> None:
    """Clear the current project."""
    config = ctx.obj["config"]
    config.current_project = None
    get_store(ctx).save(config)
    console.print("Current project cleared")


def _resolve_project_for_file(ctx: typer.Context, project: Optional[str]) -> str:
    try:
        return get_client(ctx).resolve_project(project)
    except OverleafError as exc:
        if project is None:
            raise OverleafError("missing project id; run `overleaf project list` then `overleaf project select <id>`") from exc
        raise


def _normalize_remote(path: str) -> str:
    value = path.replace("\\", "/")
    if not value.startswith("/"):
        value = "/" + value
    normalized = posixpath.normpath(value)
    return "/" if normalized == "." else normalized


def _file_cwd(ctx: typer.Context, pid: str) -> str:
    return _normalize_remote(ctx.obj["config"].defaults.get("file_cwd", {}).get(pid, "/"))


def _set_file_cwd(ctx: typer.Context, pid: str, cwd: str) -> None:
    config = ctx.obj["config"]
    config.defaults.setdefault("file_cwd", {})[pid] = _normalize_remote(cwd)
    get_store(ctx).save(config)


def _resolve_remote_path(ctx: typer.Context, pid: str, remote_path: Optional[str], default_name: str | None = None) -> str:
    raw = remote_path or default_name or "."
    if raw in {"", "."}:
        return _file_cwd(ctx, pid)
    if raw.startswith("/"):
        return _normalize_remote(raw)
    cwd = _file_cwd(ctx, pid)
    return _normalize_remote(posixpath.join(cwd, raw))


def _known_folder_paths(client: OverleafClient, pid: str) -> set[str]:
    entity_paths = {client._normalize_remote_path(entity["path"]) for entity in client.list_entities(pid)}
    folders = client._folder_paths_from_entities(entity_paths)
    folders.add("/")
    folders.update(client.config.defaults.get("folder_ids", {}).get(pid, {}).keys())
    return folders


def _ansi(text: str, *codes: str, enabled: bool = True, readline_prompt: bool = False) -> str:
    if not enabled or not codes:
        return text
    start = "".join(f"\033[{code}m" for code in codes)
    end = "\033[0m"
    if readline_prompt:
        start = f"\001{start}\002"
        end = f"\001{end}\002"
    return f"{start}{text}{end}"


def _interactive_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def _file_shell_prompt(ctx: typer.Context, pid: str) -> str:
    color = _interactive_color()
    cwd = _file_cwd(ctx, pid)
    project = pid[-6:]
    return "".join(
        [
            _ansi("overleaf", "1;36", enabled=color, readline_prompt=True),
            _ansi("@", "2", enabled=color, readline_prompt=True),
            _ansi(project, "1;35", enabled=color, readline_prompt=True),
            _ansi(":", "2", enabled=color, readline_prompt=True),
            _ansi(cwd, "1;34", enabled=color, readline_prompt=True),
            _ansi("> ", "1;32", enabled=color, readline_prompt=True),
        ]
    )


def _print_file_shell_help() -> None:
    color = _interactive_color()
    typer.echo(_ansi("Commands:", "1;36", enabled=color), color=True)
    for line in (
        "  pwd",
        "  ls [path]",
        "  cd <dir>",
        "  tree [path]",
        "  download <remote> [-o local]",
        "  upload <local> [remote]",
        "  edit <remote> [--editor vim|nano]",
        "  vim <remote>",
        "  nano <remote>",
        "  mkdir <dir>",
        "  help",
        "  exit | quit",
    ):
        typer.echo(line)


class FileShellCompleter:
    commands = ["pwd", "ls", "upload", "download", "mkdir", "tree", "cd", "edit", "vim", "nano", "help", "exit", "quit"]

    def __init__(self, ctx: typer.Context, pid: str) -> None:
        self.ctx = ctx
        self.pid = pid
        self.matches: list[str] = []

    def install(self) -> None:
        try:
            import readline
        except ImportError:
            return
        readline.set_completer_delims(" \t\n")
        readline.set_completer(self.complete)
        readline.parse_and_bind("tab: complete")

    def complete(self, text: str, state: int) -> str | None:
        if state == 0:
            self.matches = self._matches(text)
        try:
            return self.matches[state]
        except IndexError:
            return None

    def _matches(self, text: str) -> list[str]:
        try:
            import readline

            line = readline.get_line_buffer()
        except Exception:
            line = text
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            return [command + " " for command in self.commands if command.startswith(text)]
        command = parts[0]
        if command not in {"ls", "cd", "tree", "download", "mkdir", "edit", "vim", "nano"}:
            return []
        return self._remote_matches(text)

    def _remote_matches(self, text: str) -> list[str]:
        try:
            entries = get_client(self.ctx).list_project_path(self.pid, _file_cwd(self.ctx, self.pid))
        except Exception:
            return []
        matches = []
        for entry in entries:
            name = entry.name + ("/" if entry.type == "dir" else "")
            if name.startswith(text):
                matches.append(name.replace(" ", "\\ "))
        return matches


def _run_file_interactive(ctx: typer.Context) -> None:
    pid = _resolve_project_for_file(ctx, None)
    color = _interactive_color()
    typer.echo(
        f"{_ansi('Connected', '1;32', enabled=color)} project={pid} cwd={_file_cwd(ctx, pid)}. "
        "Type `help` or press Tab for commands.",
        color=True,
    )
    completer = FileShellCompleter(ctx, pid)
    completer.install()
    history = Path.home() / ".local" / "state" / "overleaf-sjtu" / "file_history"
    try:
        import readline

        history.parent.mkdir(parents=True, exist_ok=True)
        try:
            readline.read_history_file(history)
        except FileNotFoundError:
            pass
        atexit.register(readline.write_history_file, history)
    except Exception:
        pass

    root_command = typer.main.get_command(app)
    while True:
        try:
            line = input(_file_shell_prompt(ctx, pid)).strip()
        except EOFError:
            typer.echo("")
            break
        except KeyboardInterrupt:
            typer.echo("")
            continue
        if not line:
            continue
        if line in {"exit", "quit"}:
            break
        if line in {"help", "?"}:
            _print_file_shell_help()
            continue
        try:
            args = shlex.split(line)
        except ValueError as exc:
            error(str(exc))
            continue
        if args and args[0] in {"vim", "nano"}:
            editor = args[0]
            args = ["edit", *args[1:], "--editor", editor]
        try:
            root_command.main(args=["file", *args], prog_name="overleaf", standalone_mode=False)
            config = get_store(ctx).load()
            ctx.obj["config"] = config
            get_client(ctx).config = config
        except click.ClickException as exc:
            exc.show()
        except click.Abort:
            error("Aborted")
        except click.exceptions.Exit:
            pass
        except AuthRequired as exc:
            error(str(exc))
        except OverleafError as exc:
            error(str(exc))


@file_app.callback()
def file_main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if sys.stdin.isatty() and sys.stdout.isatty():
        _run_file_interactive(ctx)
        raise typer.Exit()
    click.echo(ctx.get_help(), color=ctx.color)
    raise typer.Exit()


def _file_ls(
    ctx: typer.Context,
    path: str,
    project: Optional[str],
    json_: bool,
    quiet: bool,
) -> None:
    pid = _resolve_project_for_file(ctx, project)
    remote = _resolve_remote_path(ctx, pid, path)
    entries = get_client(ctx).list_project_path(pid, remote)
    if json_:
        emit_json(entries)
    else:
        emit_file_entries(entries, quiet=quiet)


def _file_download(ctx: typer.Context, remote_path: str, output: Optional[Path], project: Optional[str], quiet: bool) -> None:
    pid = _resolve_project_for_file(ctx, project)
    remote = _resolve_remote_path(ctx, pid, remote_path)
    path = get_client(ctx).download_project_path(pid, remote, output)
    typer.echo(str(path) if quiet else f"Saved {path}")


def _file_upload(ctx: typer.Context, local_path: Path, remote_path: Optional[str], project: Optional[str], json_: bool) -> None:
    pid = _resolve_project_for_file(ctx, project)
    remote = _resolve_remote_path(ctx, pid, remote_path, local_path.name)
    uploaded = get_client(ctx).upload_file_path(pid, local_path, remote)
    if json_:
        emit_json(uploaded)
    else:
        for entry in uploaded:
            console.print(f"Uploaded {entry.path}")


def _file_mkdir(ctx: typer.Context, remote_path: str, project: Optional[str], json_: bool) -> None:
    pid = _resolve_project_for_file(ctx, project)
    remote = _resolve_remote_path(ctx, pid, remote_path)
    data = get_client(ctx).create_folder(pid, remote)
    if json_:
        emit_json(data)
    else:
        console.print(f"Created {remote}")


def _file_edit(ctx: typer.Context, remote_path: str, editor: Optional[str], project: Optional[str]) -> None:
    pid = _resolve_project_for_file(ctx, project)
    remote = _resolve_remote_path(ctx, pid, remote_path)
    if not get_client(ctx)._is_editable_doc_path(posixpath.basename(remote)):
        raise OverleafError(f"remote file does not look like an editable text doc: {remote}")
    editor_cmd = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    with tempfile.TemporaryDirectory(prefix="overleaf-edit-") as tmp:
        local = Path(tmp) / posixpath.basename(remote)
        try:
            downloaded = get_client(ctx).download_project_path(pid, remote, local)
            if downloaded.is_dir():
                raise OverleafError(f"remote path is a directory: {remote}")
        except OverleafError as exc:
            if "remote path not found" not in str(exc):
                raise
            local.write_text("")
        before = hashlib.sha256(local.read_bytes()).hexdigest()
        command = [*shlex.split(editor_cmd), str(local)]
        proc = subprocess.run(command)
        if proc.returncode != 0:
            raise OverleafError(f"editor exited with status {proc.returncode}: {editor_cmd}")
        after = hashlib.sha256(local.read_bytes()).hexdigest()
        if before == after:
            console.print(f"No changes: {remote}")
            return
        uploaded = get_client(ctx).upload_file_path(pid, local, remote)
        for entry in uploaded:
            console.print(f"Uploaded {entry.path}")


@file_app.command("ls")
def file_ls(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Remote project path. Relative paths use `overleaf file pwd`."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only paths."),
) -> None:
    """List files under a project path."""
    _file_ls(ctx, path, project, json_, quiet)


@file_app.command("pwd")
def file_pwd(
    ctx: typer.Context,
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
) -> None:
    """Print the current remote directory for a project."""
    pid = _resolve_project_for_file(ctx, project)
    console.print(_file_cwd(ctx, pid))


@file_app.command("cd")
def file_cd(
    ctx: typer.Context,
    path: str = typer.Argument("/", help="Remote directory path."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
) -> None:
    """Change the remembered remote directory for a project."""
    pid = _resolve_project_for_file(ctx, project)
    remote = _resolve_remote_path(ctx, pid, path)
    if remote not in _known_folder_paths(get_client(ctx), pid):
        raise typer.BadParameter(f"remote directory not found: {remote}")
    _set_file_cwd(ctx, pid, remote)
    console.print(remote)


@file_app.command("download")
def file_download(
    ctx: typer.Context,
    remote_path: str = typer.Argument(..., help="Remote file or directory path."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Local destination path."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only output path."),
) -> None:
    """Download a file or directory from the current project."""
    _file_download(ctx, remote_path, output, project, quiet)


@file_app.command("upload")
def file_upload(
    ctx: typer.Context,
    local_path: Path = typer.Argument(..., exists=True, readable=True, help="Local file or directory."),
    remote_path: Optional[str] = typer.Argument(None, help="Remote destination path. Defaults to /LOCAL_NAME."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Upload a local file or directory into the current project."""
    _file_upload(ctx, local_path, remote_path, project, json_)


@file_app.command("edit")
def file_edit(
    ctx: typer.Context,
    remote_path: str = typer.Argument(..., help="Remote text file path."),
    editor: Optional[str] = typer.Option(None, "--editor", "-e", help="Editor command. Defaults to $VISUAL, $EDITOR, or nano."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
) -> None:
    """Edit a remote text file locally and upload it back if changed."""
    _file_edit(ctx, remote_path, editor, project)


@file_app.command("mkdir")
def file_mkdir(
    ctx: typer.Context,
    remote_path: str = typer.Argument(..., help="Remote directory path."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Create a remote project directory."""
    _file_mkdir(ctx, remote_path, project, json_)


@file_app.command("tree")
def file_tree(
    ctx: typer.Context,
    path: str = typer.Argument(".", help="Remote directory path."),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
    depth: Optional[int] = typer.Option(None, "--depth", min=0, help="Maximum tree depth below PATH. Defaults to 3 for text output."),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, help="Maximum entries to print. Defaults to 200 for text output."),
    all_: bool = typer.Option(False, "--all", help="Print the full tree."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Print a recursive file tree from a project path."""
    if all_ and (depth is not None or limit is not None):
        raise typer.BadParameter("use either --all or --depth/--limit")
    pid = _resolve_project_for_file(ctx, project)
    base = _resolve_remote_path(ctx, pid, path)
    entities = get_client(ctx).list_entities(pid)
    entries = []
    max_depth = None if all_ or json_ else 3
    if depth is not None:
        max_depth = depth
    for entity in entities:
        remote = _normalize_remote(str(entity["path"]))
        if base != "/" and not (remote == base or remote.startswith(base.rstrip("/") + "/")):
            continue
        rel = remote.strip("/") if base == "/" else remote[len(base.rstrip("/") + "/") :]
        parts = [part for part in rel.split("/") if part]
        if max_depth is not None and len(parts) > max_depth:
            continue
        entries.append({"path": remote, "type": str(entity.get("type") or "file")})
    max_entries = None if all_ or json_ else 200
    if limit is not None:
        max_entries = limit
    if json_:
        if max_entries is not None:
            entries = entries[:max_entries]
        emit_json(entries)
        return
    names: set[str] = set()
    for entry in entries:
        rel = entry["path"].strip("/") if base == "/" else entry["path"][len(base.rstrip("/") + "/") :]
        parts = [part for part in rel.split("/") if part]
        prefix = ""
        for index, part in enumerate(parts):
            prefix = f"{prefix}/{part}" if prefix else part
            names.add(prefix + ("/" if index < len(parts) - 1 else ""))
    console.print(base)
    sorted_names = sorted(names, key=lambda item: (item.count("/"), item.rstrip("/").lower()))
    shown_names = sorted_names if max_entries is None else sorted_names[:max_entries]
    for name in shown_names:
        depth = name.rstrip("/").count("/")
        console.print(f"{'  ' * depth}{posixpath.basename(name.rstrip('/'))}{'/' if name.endswith('/') else ''}")
    hidden = len(sorted_names) - len(shown_names)
    if hidden > 0:
        console.print(f"... {hidden} more entries. Use `overleaf file tree --all` or raise `--limit`.")


_FILE_COMMAND_ORDER = {"pwd": 0, "ls": 1, "upload": 2, "download": 3, "edit": 4, "mkdir": 5, "tree": 6, "cd": 7}
file_app.registered_commands.sort(key=lambda command: _FILE_COMMAND_ORDER.get(command.name or "", 100))


@compile_app.command("run")
def compile_run(
    ctx: typer.Context,
    project: Optional[str] = typer.Argument(None, help="Project ID or URL. Defaults to current project."),
    draft: bool = typer.Option(False, "--draft", help="Compile in draft mode."),
    stop_on_first_error: bool = typer.Option(False, "--stop-on-first-error", help="Stop after first LaTeX error."),
    timeout_seconds: int = typer.Option(120, "--wait", min=1, help="Seconds to wait for completion."),
    compiler: Optional[Compiler] = typer.Option(
        None,
        "--compiler",
        help="Temporarily run with this compiler. Persist the default with `overleaf settings compiler [latex|lualatex|pdflatex|xelatex]`.",
    ),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    client = get_client(ctx)
    pid = client.resolve_project(project)
    compiler_value = compiler.value if compiler else None
    current_compiler = compiler_value or client.get_compiler(pid)
    if not json_:
        source = "override" if compiler_value else "setting"
        if current_compiler:
            console.print(f"{pid}: compiler={current_compiler} ({source})")
        else:
            console.print(f"{pid}: compiler=unknown")
            console.print("Set one with: overleaf settings compiler [latex|lualatex|pdflatex|xelatex]")
    result = client.compile(
        pid,
        draft=draft,
        timeout_seconds=timeout_seconds,
        stop_on_first=stop_on_first_error,
        compiler=compiler_value,
    )
    if json_:
        emit_json(result)
    else:
        console.print(f"{pid}: {result.status}")
        if result.pdf_url:
            console.print(f"pdf: {result.pdf_url}")
        command_project = "" if project is None else f" {pid}"
        console.print("Next:")
        console.print(f"  overleaf compile log{command_project}")
        console.print(f"  overleaf compile pdf{command_project} -o output.pdf")


@compile_app.command("status")
def compile_status(
    ctx: typer.Context,
    project: Optional[str] = typer.Argument(None, help="Project ID or URL. Defaults to current project."),
    json_: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    pid = get_client(ctx).resolve_project(project)
    result = get_client(ctx).compile_status(pid)
    if json_:
        emit_json(result or {"project_id": pid, "status": "unknown"})
    else:
        console.print(f"{pid}: {result.status if result else 'unknown'}")


@compile_app.command("pdf")
def compile_pdf(
    ctx: typer.Context,
    project: Optional[str] = typer.Argument(None, help="Project ID or URL. Defaults to current project."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination PDF path."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only output path."),
) -> None:
    pid = get_client(ctx).resolve_project(project)
    output_path = output or Path(f"{pid}.pdf")
    path = get_client(ctx).download_pdf(pid, output_path)
    typer.echo(str(path) if quiet else f"Saved {path}")


@compile_app.command("log")
def compile_log(
    ctx: typer.Context,
    project: Optional[str] = typer.Argument(None, help="Project ID or URL. Defaults to current project."),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write log to file."),
    tail: int = typer.Option(80, "--tail", min=1, help="Print only the last N lines."),
    full: bool = typer.Option(False, "--full", help="Print the full log to stdout."),
) -> None:
    if full and tail != 80:
        raise typer.BadParameter("use either --full or --tail, not both")
    pid = get_client(ctx).resolve_project(project)
    text = get_client(ctx).fetch_log(pid)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
        console.print(f"Saved {output}")
        return
    lines = text.splitlines()
    if not full:
        lines = lines[-tail:]
        console.print(f"Showing last {tail} log lines. Use `overleaf compile log --full` or `overleaf compile log -o output.log` for the full log.")
    typer.echo("\n".join(lines))


@settings_app.command("compiler")
def settings_compiler(
    ctx: typer.Context,
    compiler: Optional[Compiler] = typer.Argument(
        None,
        help="Set the persistent project compiler: overleaf settings compiler [latex|lualatex|pdflatex|xelatex].",
    ),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Project ID or URL. Defaults to current project."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only the compiler."),
) -> None:
    pid = get_client(ctx).resolve_project(project)
    if compiler is None:
        current = get_client(ctx).get_compiler(pid)
        if not current:
            raise typer.BadParameter(
                "compiler is unknown; set one with `overleaf settings compiler [latex|lualatex|pdflatex|xelatex]`"
            )
        console.print(current if quiet else f"{pid}: compiler={current}")
        return
    compiler_value = compiler.value
    get_client(ctx).set_compiler(pid, compiler_value)
    console.print(f"{pid}: compiler={compiler_value}")


def run() -> None:
    try:
        _normalize_completion_env()
        app()
    except AuthRequired as exc:
        error(str(exc))
        sys.exit(2)
    except OverleafError as exc:
        error(str(exc))
        sys.exit(1)


def _normalize_completion_env() -> None:
    instruction = os.environ.get("_OVERLEAF_COMPLETE")
    legacy = {
        "bash_complete": "complete_bash",
        "zsh_complete": "complete_zsh",
    }
    if instruction in legacy:
        os.environ["_OVERLEAF_COMPLETE"] = legacy[instruction]


if __name__ == "__main__":
    run()
