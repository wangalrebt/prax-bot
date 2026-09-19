"""Repository packaging, path rules, and Git change collection shared by both backends."""

from __future__ import annotations

import io
import posixpath
import re
import shlex
import subprocess
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

from prax_bot.settings import WORKDIR
from prax_bot.workspace.types import Changes, Workspace, WorkspaceError

MAX_REPO_BYTES = 200 * 1024 * 1024


DIFF_OUTPUT_LIMIT = 8 * 1024 * 1024  # the sandbox API rejects truncate_output_at of 10 MiB or more


DIFF_EXCLUDES = (
    "__pycache__/",
    "*.pyc",
    "*.egg-info/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "build/",
    "dist/",
    ".venv/",
    "venv/",
    "node_modules/",
    ".tox/",
    ".coverage",
    "htmlcov/",
)


def git_exclude_command() -> str:
    """Shell snippet that appends DIFF_EXCLUDES to .git/info/exclude in the current directory."""
    body = "\\n".join(DIFF_EXCLUDES)
    return f"mkdir -p .git/info && printf '%b\\n' '{body}' >> .git/info/exclude"


SUSPICIOUS_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.keystore",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials*.json",
    "service-account*.json",
)


SCRATCH_DIR = "/tmp/praxbot/scratch"  # throwaway scripts live here, outside the repository and the diff
SCRATCH_NAME_RE = re.compile(r"^(reproduce|repro|scratch|debug|tmp|temp)[^/]*$|^test_[^/]*\.py$|^[^/]*_test\.py$")


def scratch_files(new_paths: Sequence[str]) -> list[str]:
    """New repository-root files that look like reproduction or debugging scripts."""
    return [p for p in new_paths if "/" not in p and SCRATCH_NAME_RE.match(p)]


def resolve_in_workdir(path: str, workdir: str = WORKDIR) -> str:
    """Map a model-supplied path onto an absolute path inside the sandbox workdir or the scratch dir.

    Raises ValueError for paths that would escape both.
    """
    raw = (path or ".").strip()
    candidate = posixpath.normpath(raw if raw.startswith("/") else posixpath.join(workdir, raw))
    for root in (workdir, SCRATCH_DIR):
        if candidate == root or candidate.startswith(root + "/"):
            return candidate
    raise ValueError(f"path {path!r} is outside the repository ({workdir}); throwaway files belong in {SCRATCH_DIR}")


def repo_file_names(repo_root: Path) -> list[str]:
    """Tracked files plus untracked files that are not ignored, as git sees them."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "-co", "--exclude-standard"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout
    return [n for n in out.decode("utf-8", "surrogateescape").split("\0") if n]


def suspicious_files(names: list[str]) -> list[str]:
    """Names that look like credentials, matched on the file's basename."""
    import fnmatch

    found = []
    for name in names:
        base = posixpath.basename(name)
        if any(fnmatch.fnmatch(base, pat) for pat in SUSPICIOUS_PATTERNS):
            found.append(name)
    return found


def pack_repo(repo_root: Path) -> bytes:
    """Tar the working tree: tracked files plus untracked files that are not ignored.

    The .git directory is not included; the sandbox creates its own baseline commit.
    """
    names = repo_file_names(repo_root)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in names:
            p = repo_root / name
            if p.is_symlink() or p.is_file():
                tar.add(p, arcname=name, recursive=False)
    data = buf.getvalue()
    if len(data) > MAX_REPO_BYTES:
        raise WorkspaceError(
            f"repository is {len(data) / 1e6:.0f} MB; Prax Bot uploads at most {MAX_REPO_BYTES / 1e6:.0f} MB"
        )
    return data


def collect_changes(workspace: Workspace, paths: Sequence[str] | None = None) -> Changes:
    """Read Git's changes; only Git's ignore rules exclude files.

    New root-level files named like reproduction scripts, and new binary files, stay in the
    diff but are flagged: an apply leaves them out unless they are named. Explicit path
    selection must name files in the current diff; literal pathspecs keep filenames from
    being interpreted as globs.
    """
    image = workspace.image_id

    def read(command: str) -> str:
        result = workspace.run(command, timeout=120, mutate=False, truncate_output_at=DIFF_OUTPUT_LIMIT)
        if result.exit_code != 0 or result.timed_out:
            raise WorkspaceError(f"could not collect changes: {result.stderr.strip() or result.stdout.strip()}")
        if result.truncated:
            raise WorkspaceError(f"changes exceed the {DIFF_OUTPUT_LIMIT // (1024 * 1024)} MiB output limit")
        return result.stdout

    metadata = read("git add -A && git rev-parse HEAD && git diff --cached --name-status -z --no-renames HEAD")
    base, separator, listing = metadata.partition("\n")
    if not separator or not base or any(c not in "0123456789abcdef" for c in base):
        raise WorkspaceError("could not read the Git baseline")
    entries = [e for e in listing.split("\0") if e]
    files = tuple(entries[1::2])
    added = {name for status, name in zip(entries[0::2], entries[1::2], strict=True) if status == "A"}
    binary: tuple[str, ...] = ()
    if added:
        numstat = read("git add -A && git diff --cached --numstat -z --no-renames HEAD")
        binary = tuple(
            parts[2]
            for line in numstat.split("\0")
            if len(parts := line.split("\t", 2)) == 3 and parts[0] == parts[1] == "-" and parts[2] in added
        )
    scratch = tuple(scratch_files(sorted(added)))
    if paths is not None:
        unknown = set(paths) - set(files)
        if unknown:
            raise ValueError(f"these files have no pending changes: {', '.join(sorted(unknown))}")
        selected = set(paths)
        files = tuple(name for name in files if name in selected)
        scratch = tuple(name for name in scratch if name in selected)
        binary = tuple(name for name in binary if name in selected)
    excluded = tuple(
        name for name in read("git ls-files --others --ignored --exclude-standard --directory -z").split("\0") if name
    )
    patch = ""
    if files:
        arguments = " ".join(shlex.quote(name) for name in files) if paths is not None else "."
        patch = read(f"git add -A && git --literal-pathspecs diff --cached --binary --no-renames HEAD -- {arguments}")
    if workspace.image_id != image:
        raise WorkspaceError("the workspace changed while collecting the diff; refresh it before applying")
    return Changes(patch=patch, base=base, image=image, files=files, excluded=excluded, scratch=scratch, binary=binary)


def advance_baseline(workspace: Workspace, changes: Changes) -> None:
    """Commit exactly the patch applied to the user's tree, leaving other work pending.

    A private index starts from the patch's original baseline. Retrying after a
    transport failure is safe when the resulting tree is already HEAD.
    """
    workspace.write(f"{SCRATCH_DIR}/applied.patch", changes.patch.encode("utf-8"))
    command = f"""set -e
prax_index=$(mktemp "${{TMPDIR:-/tmp}}/praxbot-index.XXXXXX")
trap 'rm -f "$prax_index"' EXIT
GIT_INDEX_FILE="$prax_index" git read-tree {shlex.quote(changes.base)}
GIT_INDEX_FILE="$prax_index" git apply --cached --binary --whitespace=nowarn "${{TMPDIR:-/tmp}}/praxbot/scratch/applied.patch"
prax_tree=$(GIT_INDEX_FILE="$prax_index" git write-tree)
if [ "$(git rev-parse HEAD^{{tree}})" != "$prax_tree" ]; then
    prax_commit=$(printf '%s\\n' 'praxbot: applied to the working tree' | git -c user.email=praxbot@localhost -c user.name=praxbot commit-tree "$prax_tree" -p {shlex.quote(changes.base)})
    git update-ref HEAD "$prax_commit" {shlex.quote(changes.base)}
fi
git read-tree HEAD
rm -f "${{TMPDIR:-/tmp}}/praxbot/scratch/applied.patch"
"""
    result = workspace.run(
        command, timeout=120, mutate=True, label=f"apply: baseline advanced from {changes.base[:12]}"
    )
    if result.exit_code != 0 or result.timed_out:
        raise WorkspaceError(
            "the working tree was updated, but the workspace baseline could not be recorded: "
            + (result.stderr or result.stdout)[-500:]
            + ". Retry /apply to finish recording it."
        )


PATCH_REJECTED = "praxbot: the diff does not apply to the clean image"


def apply_patch_shell(path: str, *, skip_if_applied: bool = False) -> str:
    """Shell that applies the patch file at `path` to the working tree and removes it; exit 97 names a rejected patch.

    With skip_if_applied a patch whose reverse applies cleanly counts as done, so an interrupted apply can be retried.
    """
    quoted = shlex.quote(path)
    apply = f"git apply --binary --whitespace=nowarn {quoted}"
    if skip_if_applied:
        apply = f"{{ git apply --reverse --check {quoted} 2>/dev/null || {apply}; }}"
    return f"{apply} || {{ echo {shlex.quote(PATCH_REJECTED)} >&2; rm -f {quoted}; exit 97; }}; rm -f {quoted}"


def apply_diff(root: Path, diff: str) -> None:
    """Apply a checked patch to the user's working tree without touching their index."""
    with tempfile.NamedTemporaryFile("w", suffix=".patch", encoding="utf-8") as patch:
        patch.write(diff)
        patch.flush()
        for args in (["git", "apply", "--check", patch.name], ["git", "apply", patch.name]):
            result = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
            if result.returncode:
                raise WorkspaceError(f"the diff does not apply cleanly: {result.stderr.strip()}")
