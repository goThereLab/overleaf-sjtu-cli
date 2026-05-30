from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    owner: Optional[str] = None
    last_updated: Optional[str] = None
    raw: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class CompileResult:
    project_id: str
    status: str
    pdf_url: Optional[str] = None
    log_url: Optional[str] = None
    output_files: tuple[dict[str, Any], ...] = ()
    raw: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class FileEntry:
    path: str
    name: str
    type: str
    raw: Optional[dict[str, Any]] = None
