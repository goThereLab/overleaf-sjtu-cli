from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://latex.sjtu.edu.cn"
DEFAULT_PROJECT_URL = f"{DEFAULT_BASE_URL}/project"
APP_NAME = "overleaf-sjtu"


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def _xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    project_url: str = DEFAULT_PROJECT_URL
    current_project: str | None = None
    defaults: dict[str, Any] = field(default_factory=dict)


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _xdg_config_home() / APP_NAME / "config.json"

    @property
    def cookie_path(self) -> Path:
        return _xdg_state_home() / APP_NAME / "cookies.json"

    @property
    def login_state_path(self) -> Path:
        return _xdg_state_home() / APP_NAME / "login_state.json"

    @property
    def login_flow_path(self) -> Path:
        return _xdg_state_home() / APP_NAME / "login_flow.json"

    def load(self) -> Config:
        if not self.path.exists():
            return Config()
        data = json.loads(self.path.read_text())
        return Config(**data)

    def save(self, config: Config) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n")

    def clear_cookies(self) -> None:
        if self.cookie_path.exists():
            self.cookie_path.unlink()

    def clear_login_state(self) -> None:
        if self.login_state_path.exists():
            self.login_state_path.unlink()
        if self.login_flow_path.exists():
            self.login_flow_path.unlink()
