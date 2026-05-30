from pathlib import Path

import pytest

from overleaf_sjtu.client import OverleafClient, OverleafError
from overleaf_sjtu.config import Config, ConfigStore


class FakeClient(OverleafClient):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(Config(current_project="0123456789abcdefabcdefab"), ConfigStore(tmp_path / "config.json"))

    def list_entities(self, project: str) -> list[dict]:
        return [
            {"path": "/main.tex", "type": "doc"},
            {"path": "/figures/a.png", "type": "file"},
            {"path": "/figures/nested/b.dat", "type": "file"},
        ]


def test_list_project_path_synthesizes_directories(tmp_path: Path) -> None:
    client = FakeClient(tmp_path)

    root = client.list_project_path("0123456789abcdefabcdefab", "/")
    assert [(item.type, item.path) for item in root] == [("dir", "/figures"), ("doc", "/main.tex")]

    figures = client.list_project_path("0123456789abcdefabcdefab", "/figures")
    assert [(item.type, item.path) for item in figures] == [("dir", "/figures/nested"), ("file", "/figures/a.png")]


def test_safe_join_rejects_zip_traversal(tmp_path: Path) -> None:
    client = FakeClient(tmp_path)

    with pytest.raises(OverleafError):
        client._safe_join(tmp_path, "../escape.txt")
