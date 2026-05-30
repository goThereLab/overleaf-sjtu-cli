from __future__ import annotations

import json
import html as html_lib
import re
from typing import Any

from bs4 import BeautifulSoup

from .models import Project

PROJECT_ID_RE = re.compile(r"/project/([0-9a-fA-F]{24})")


def extract_csrf(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "ol-csrfToken"}) or soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return str(meta["content"])
    match = re.search(r'csrfToken["\']?\s*[:=]\s*["\']([^"\']+)', html)
    if match:
        return match.group(1)
    match = re.search(r'name=["\']_csrf["\']\s+value=["\']([^"\']+)', html)
    return match.group(1) if match else None


def extract_bootstrap_json(html: str) -> dict[str, Any]:
    patterns = [
        r"window\.data\s*=\s*({.*?});\s*</script>",
        r"window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>",
        r"window\.metaAttributes\s*=\s*({.*?});\s*</script>",
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.S)
        if not match:
            continue
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    return {}


def extract_meta_json(html: str, name: str) -> Any:
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": name, "data-type": "json"}) or soup.find("meta", attrs={"name": name})
    if not meta or not meta.get("content"):
        return None
    content = html_lib.unescape(str(meta["content"]))
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def extract_jaccount_login_context(html: str) -> dict[str, str]:
    match = re.search(r"var\s+loginContext\s*=\s*\{(.*?)\};", html, re.S)
    if not match:
        return {}
    body = match.group(1)
    context: dict[str, str] = {}
    for key in ("loginType", "sid", "client", "returl", "se", "v", "uuid"):
        value_match = re.search(rf"{key}\s*:\s*[\"']([^\"']*)[\"']", body)
        if value_match:
            context[key] = value_match.group(1)
    return context


def page_requires_captcha(html: str) -> bool:
    return "setCaptchaCheckStatus('failed')" in html or 'setCaptchaCheckStatus("failed")' in html


def projects_from_json(data: Any) -> list[Project]:
    found: list[Project] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "id" in node and ("name" in node or "title" in node):
                pid = str(node.get("id") or node.get("_id"))
                if re.fullmatch(r"[0-9a-fA-F]{24}", pid):
                    found.append(
                        Project(
                            id=pid,
                            name=str(node.get("name") or node.get("title") or pid),
                            owner=_str_or_none(node.get("owner") or node.get("owner_ref")),
                            last_updated=_str_or_none(
                                node.get("lastUpdated") or node.get("last_updated") or node.get("updatedAt")
                            ),
                            raw=node,
                        )
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    dedup: dict[str, Project] = {}
    for project in found:
        dedup[project.id] = project
    return list(dedup.values())


def parse_project_list(html: str) -> list[Project]:
    projects = projects_from_json(extract_meta_json(html, "ol-prefetchedProjectsBlob"))
    if projects:
        return projects

    projects = projects_from_json(extract_bootstrap_json(html))
    if projects:
        return projects

    soup = BeautifulSoup(html, "html.parser")
    by_id: dict[str, Project] = {}
    for anchor in soup.find_all("a", href=True):
        match = PROJECT_ID_RE.search(str(anchor["href"]))
        if not match:
            continue
        pid = match.group(1)
        name = " ".join(anchor.get_text(" ", strip=True).split()) or pid
        by_id[pid] = Project(id=pid, name=name)
    return list(by_id.values())


def parse_project_id(value: str) -> str:
    match = re.search(r"([0-9a-fA-F]{24})", value)
    if not match:
        raise ValueError(f"not a project id or URL: {value}")
    return match.group(1)


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("email", "name", "first_name"):
            if value.get(key):
                return str(value[key])
        return None
    return str(value)
