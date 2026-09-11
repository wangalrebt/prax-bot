"""Recover applying a patch across the host tree and the workspace baseline."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from prax_bot.runtime.storage import write_json
from prax_bot.workspace.repository import advance_baseline, apply_diff
from prax_bot.workspace.types import Changes, Workspace, WorkspaceError


def file_states(root: Path, files: tuple[str, ...]) -> dict[str, str | None]:
    """Fingerprint the file contents and Git-relevant modes, including deletions."""
    states: dict[str, str | None] = {}
    for name in files:
        path = root / name
        if path.is_symlink():
            states[name] = "symlink:" + os.readlink(path)
        elif path.is_file():
            mode = "executable:" if path.stat().st_mode & 0o100 else "file:"
            states[name] = mode + hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            children = file_states(path, tuple(sorted(child.name for child in path.iterdir())))
            states[name] = "directory:" + hashlib.sha256(json.dumps(children, sort_keys=True).encode()).hexdigest()
        elif path.exists():
            raise WorkspaceError(f"cannot apply changes: {name} is not a file")
        else:
            states[name] = None
    return states


@dataclass
class ApplyTransaction:
    directory: Path
    changes: Changes
    before: dict[str, str | None]
    after: dict[str, str | None]
    host_applied: bool = False

    @property
    def metadata(self) -> dict[str, Any]:
        return {"files": list(self.changes.files), "host_applied": self.host_applied}

    def save(self) -> None:
        write_json(
            self.directory / "apply.json",
            {
                "changes": dataclasses.asdict(self.changes),
                "before": self.before,
                "after": self.after,
                "host_applied": self.host_applied,
            },
        )

    @classmethod
    def prepare(cls, directory: Path, root: Path, changes: Changes) -> ApplyTransaction:
        """Check the patch on copies and persist intent before changing the host."""
        with tempfile.TemporaryDirectory(prefix="praxbot-apply-") as temporary:
            staging = Path(temporary)
            for name in changes.files:
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise WorkspaceError(f"invalid patch path: {name}")
                source, target = root / name, staging / name
                if target.exists() or target.is_symlink():
                    continue  # an affected parent directory already copied this entry
                if source.is_file() or source.is_symlink():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target, follow_symlinks=False)
                elif source.is_dir():
                    shutil.copytree(source, target, symlinks=True)
                elif source.exists():
                    raise WorkspaceError(f"cannot apply changes: {name} is not a file")
            before = file_states(staging, changes.files)
            apply_diff(staging, changes.patch)
            after = file_states(staging, changes.files)
        transaction = cls(directory, changes, before, after)
        transaction.save()
        return transaction

    @classmethod
    def load(cls, directory: Path, record: dict[str, Any]) -> ApplyTransaction | None:
        journal = directory / "apply.json"
        if journal.exists():
            data = json.loads(journal.read_text(encoding="utf-8"))
            changes = data.pop("changes")
            changes["files"] = tuple(changes["files"])
            changes["excluded"] = tuple(changes.get("excluded", ()))
            return cls(directory, Changes(**changes), **data)
        # Older runs wrote their pending patch only after updating the host.
        legacy = record.get("pending_apply")
        if legacy:
            changes = Changes(patch=(directory / "pending-apply.patch").read_text(encoding="utf-8"), **legacy)
            return cls(directory, changes, {}, {}, host_applied=True)
        return None

    def finish(self, root: Path, workspace: Workspace) -> None:
        """Reconcile an interrupted host apply, then advance the exact baseline."""
        if not self.host_applied:
            current = file_states(root, self.changes.files)
            if current == self.before:
                apply_diff(root, self.changes.patch)
                current = file_states(root, self.changes.files)
            if current != self.after:
                raise WorkspaceError(
                    "the files changed during an unfinished apply; restore the affected files to their "
                    "pre-apply or applied contents, then retry /apply"
                )
            self.host_applied = True
            self.save()
        advance_baseline(workspace, self.changes)

    def clear(self) -> None:
        (self.directory / "pending-apply.patch").unlink(missing_ok=True)
        (self.directory / "apply.json").unlink(missing_ok=True)
