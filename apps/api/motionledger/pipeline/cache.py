from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..meta.client import canonical_hash


class ArtifactCache:
    """Small content-addressed, filesystem cache for immutable pipeline artifacts."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(stage: str, inputs: dict[str, Any]) -> str:
        return canonical_hash({"stage": stage, "inputs": inputs})

    def directory(self, key: str) -> Path:
        return self.root / key[:2] / key

    def artifact(self, key: str, name: str) -> Path:
        # Artifact names are internal fixed names, but reject path traversal regardless.
        if Path(name).name != name or name in {"", ".", ".."}:
            raise ValueError("Invalid cache artifact name")
        return self.directory(key) / name

    def read_json(self, key: str, name: str = "result.json") -> dict[str, Any] | None:
        path = self.artifact(key, name)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def write_json(self, key: str, payload: dict[str, Any], name: str = "result.json") -> Path:
        path = self.artifact(key, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            json.dump(payload, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp.flush()
            temp_path = Path(tmp.name)
        temp_path.replace(path)
        return path

    def has_files(self, key: str, names: tuple[str, ...] | list[str]) -> bool:
        return all(self.artifact(key, name).is_file() for name in names)

    def store_file(self, key: str, name: str, source: Path) -> Path:
        if not source.is_file():
            raise FileNotFoundError(source)
        target = self.artifact(key, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            shutil.copyfile(source, temp_path)
            temp_path.replace(target)
        finally:
            temp_path.unlink(missing_ok=True)
        return target

    def copy_file(self, key: str, name: str, destination: Path) -> bool:
        source = self.artifact(key, name)
        if not source.is_file():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            shutil.copyfile(source, temp_path)
            temp_path.replace(destination)
        finally:
            temp_path.unlink(missing_ok=True)
        return True
