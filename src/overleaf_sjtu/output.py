from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def emit_json(value: Any) -> None:
    def default(obj: Any) -> Any:
        if is_dataclass(obj):
            return asdict(obj)
        raise TypeError(type(obj).__name__)

    print(json.dumps(value, default=default, ensure_ascii=False, indent=2))


def emit_projects(projects: list, quiet: bool = False) -> None:
    if quiet or not sys.stdout.isatty():
        for project in projects:
            print(project.id)
        return
    print("ID\tName\tUpdated\tOwner")
    for project in projects:
        print(f"{project.id}\t{project.name}\t{project.last_updated or ''}\t{project.owner or ''}")


def emit_file_entries(entries: list, quiet: bool = False) -> None:
    if quiet or not sys.stdout.isatty():
        for entry in entries:
            print(entry.path)
        return
    print("TYPE\tPATH")
    for entry in entries:
        print(f"{entry.type}\t{entry.path}")


def warn(message: str) -> None:
    err_console.print(f"[yellow]{message}[/yellow]")


def error(message: str) -> None:
    err_console.print(f"[red]{message}[/red]")
