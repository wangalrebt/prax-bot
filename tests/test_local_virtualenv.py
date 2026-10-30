from __future__ import annotations

import json
import os
import py_compile
import shlex
import subprocess
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from prax_bot.runtime.runs import load_run, reopen_workspace
from prax_bot.workspace.local import LocalWorkspace, clone_virtualenv
from tests.helpers import make_session


def install_probe(environment: Path) -> Path:
    """A real installed console script, without downloading a package."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    result = subprocess.run(
        [str(environment / "bin" / "python"), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True,
        capture_output=True,
        text=True,
    )
    package = Path(result.stdout.strip()) / "prax_clone_probe.py"
    package.write_text("VALUE = 'original'\n", encoding="utf-8")
    launcher = environment / "bin" / "clone-probe"
    launcher.write_text(
        f"#!{environment / 'bin' / 'python'}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "import prax_clone_probe\n"
        "if len(sys.argv) > 1:\n"
        "    Path(prax_clone_probe.__file__).write_text(\"VALUE = 'candidate'\\n\")\n"
        "print(json.dumps([sys.prefix, prax_clone_probe.VALUE]))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return package


def test_reopened_candidates_use_independent_environments_and_preserve_clean_snapshot(tmp_path: Path) -> None:
    session = make_session(tmp_path)
    try:
        original = session.ws
        assert isinstance(original, LocalWorkspace)
        package = install_probe(original.venv)
        original.mark_clean()
        original.write("a.py", b"candidate starting point\n")
        session.record()
        run = load_run(session.run_id, session.settings)
        candidates = [
            reopen_workspace(run, original.current_step, session.settings, new_run_id=name)
            for name in ("candidate-a", "candidate-b")
        ]
        for candidate in candidates:
            assert isinstance(candidate, LocalWorkspace)
            result = candidate.run("clone-probe", mutate=False)
            assert result.exit_code == 0, result.stderr
            assert json.loads(result.stdout) == [str(candidate.venv), "original"]
            activated = candidate.run(
                f"source {shlex.quote(str(candidate.venv / 'bin' / 'activate'))}; clone-probe", mutate=False
            )
            assert activated.exit_code == 0, activated.stderr
            assert json.loads(activated.stdout) == [str(candidate.venv), "original"]
            assert candidate.read("a.py") == b"candidate starting point\n"
            assert candidate.run_clean("cat a.py").stdout == "x\n"

        first, second = candidates
        assert first.run("clone-probe update", mutate=False).exit_code == 0
        assert json.loads(first.run("clone-probe", mutate=False).stdout)[1] == "candidate"
        assert json.loads(second.run("clone-probe", mutate=False).stdout)[1] == "original"
        assert package.read_text(encoding="utf-8") == "VALUE = 'original'\n"
        assert original.read("a.py") == b"candidate starting point\n"
        assert original.current_step == 1
    finally:
        session.close()


def test_virtualenv_clone_retargets_internal_links_without_rewriting_binaries_or_external_files(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    install_probe(source)
    (source / "bin" / "absolute-python").symlink_to(source / "bin" / "python")
    external = tmp_path / "external-script"
    external.write_text(f"#!{source / 'bin' / 'python'}\n", encoding="utf-8")
    (source / "bin" / "external-script").symlink_to(external)
    native = b"\0native executable " + os.fsencode(source)
    (source / "bin" / "native").write_bytes(native)

    clone_virtualenv(source, target, old_workdir=tmp_path / "old-current", new_workdir=tmp_path / "new-current")

    assert os.readlink(target / "bin" / "absolute-python") == str(target / "bin" / "python")
    assert os.readlink(target / "bin" / "python") == os.readlink(source / "bin" / "python")
    assert os.readlink(target / "bin" / "external-script") == str(external)
    assert external.read_text(encoding="utf-8") == f"#!{source / 'bin' / 'python'}\n"
    assert (target / "bin" / "native").read_bytes() == native


@pytest.mark.parametrize("editable_format", ["path", "finder"])
def test_editable_imports_follow_candidate_and_disposable_check_trees(tmp_path: Path, editable_format: str) -> None:
    session = make_session(tmp_path)
    try:
        original = session.ws
        assert isinstance(original, LocalWorkspace)
        package = install_probe(original.venv)
        module = "src/editable_probe/__init__.py"
        original.write(module, b"VALUE = 'baseline'\n")
        assert (
            original.run(
                "git add src && git -c user.name=test -c user.email=test@example.com commit -qm editable"
            ).exit_code
            == 0
        )
        original.mark_clean()
        if editable_format == "path":
            (package.parent / "editable_probe.pth").write_text(str(original.current / "src"), encoding="utf-8")
        else:
            # The generated setuptools format: a .pth imports a finder with an absolute MAPPING.
            finder_name = "__editable___probe_finder"
            finder = package.parent / f"{finder_name}.py"
            finder.write_text(
                "import sys\n"
                "from importlib.util import spec_from_file_location\n"
                f"MAPPING = {{'editable_probe': {str(original.current / 'src' / 'editable_probe')!r}}}\n"
                "class Finder:\n"
                "    @classmethod\n"
                "    def find_spec(cls, fullname, path=None, target=None):\n"
                "        if fullname in MAPPING:\n"
                "            return spec_from_file_location(fullname, MAPPING[fullname] + '/__init__.py')\n"
                "def install():\n"
                "    sys.meta_path.append(Finder)\n",
                encoding="utf-8",
            )
            py_compile.compile(str(finder), doraise=True)
            (package.parent / "__editable__.probe.pth").write_text(
                f"import {finder_name}; {finder_name}.install()\n", encoding="utf-8"
            )
        original.write(module, b"VALUE = 'original work'\n")
        session.record()
        candidate = reopen_workspace(
            load_run(session.run_id, session.settings), original.current_step, session.settings, new_run_id="candidate"
        )
        assert isinstance(candidate, LocalWorkspace)
        candidate.write(module, b"VALUE = 'candidate work'\n")
        command = "python -c 'import editable_probe, json; print(json.dumps([editable_probe.VALUE, editable_probe.__file__]))'"
        imported = candidate.run(command, mutate=False)
        assert imported.exit_code == 0, imported.stderr
        assert json.loads(imported.stdout) == ["candidate work", str(candidate.current / module)]
        assert original.read(module) == b"VALUE = 'original work'\n"

        check_code = (
            "import editable_probe, prax_clone_probe, json, os, sys; from pathlib import Path; "
            "home = Path.home() / 'check-state'; temp = Path(os.environ['TMPDIR']) / 'check-state'; "
            "print(json.dumps([editable_probe.VALUE, prax_clone_probe.VALUE, str(sys.prefix), home.exists(), temp.exists()])); "
            "Path(prax_clone_probe.__file__).write_text(\"VALUE = 'check changed dependency'\\n\"); "
            "home.touch(); temp.touch()"
        )
        check_command = "python -c " + shlex.quote(check_code)
        patch = candidate.diff()
        with ThreadPoolExecutor(max_workers=2) as workers:
            before = workers.submit(candidate.run_clean, check_command)
            after = workers.submit(candidate.run_clean, check_command, patch=patch)
            baseline, patched = before.result(), after.result()
        assert baseline.exit_code == patched.exit_code == 0, baseline.stderr + patched.stderr
        baseline_values, patched_values = json.loads(baseline.stdout), json.loads(patched.stdout)
        assert baseline_values[:2] == ["baseline", "original"]
        assert patched_values[:2] == ["candidate work", "original"]
        assert baseline_values[2] != patched_values[2]
        assert baseline_values[3:] == patched_values[3:] == [False, False]
        assert package.read_text(encoding="utf-8") == "VALUE = 'original'\n"
        assert json.loads(candidate.run("clone-probe", mutate=False).stdout)[1] == "original"
        assert not (candidate.root / "home" / "check-state").exists()
        assert not (candidate.root / "tmp" / "check-state").exists()
        assert candidate.read(module) == b"VALUE = 'candidate work'\n"
    finally:
        session.close()
