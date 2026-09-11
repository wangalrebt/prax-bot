"""Atomic, durable JSON writes for run records and apply journals."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: Path, data: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as output:
        temporary = Path(output.name)
        try:
            json.dump(data, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
