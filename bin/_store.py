"""Private local-store write helpers for NockBrain artifacts."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_mkdir(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed or ".nock-brain" in path.expanduser().parts:
        path.chmod(DIR_MODE)


def secure_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    secure_mkdir(path.parent)
    path.write_text(text, encoding=encoding)
    path.chmod(FILE_MODE)


def secure_write_json(path: Path, value: Any, **json_kwargs: Any) -> None:
    secure_write_text(path, json.dumps(value, **json_kwargs))


def secure_copyfile(src: Path, dst: Path) -> None:
    secure_mkdir(dst.parent)
    shutil.copyfile(src, dst)
    dst.chmod(FILE_MODE)
