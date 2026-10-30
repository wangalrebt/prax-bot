"""Create a fresh, disposable repository for the submission's live sandbox demonstration."""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    directory = Path(tempfile.mkdtemp(prefix="prax-submission-demo-"))
    repo = directory / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef multiply(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n\nclass ArithmeticTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n"
        "        self.assertEqual(add(-2, 3), 1)\n\n\n"
        'if __name__ == "__main__":\n    unittest.main()\n',
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    for arguments in (
        ["init", "-q"],
        ["add", "."],
        ["-c", "user.name=Prax Demo", "-c", "user.email=demo@example.invalid", "commit", "-qm", "Failing fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True)
    command = [
        "env",
        f"PRAXBOT_HOME={directory / 'home'}",
        "uv",
        "run",
        "--project",
        str(Path(__file__).resolve().parents[1]),
        "prax",
        "web",
        "--repo",
        str(repo),
        "--backend",
        "contree",
        "--base-image",
        "python:3.12",
        "--effort",
        "low",
        "--max-steps",
        "12",
        "--setup",
        "",
        "--test-command",
        "python -B -m unittest -v",
        "--lint-command",
        "",
        "--protect",
        "test_calc.py",
    ]
    print(f"Demo repository: {repo}")
    print(f"Session records: {directory / 'home'}")
    print("Start the live sandbox demo (uses PRAXBOT_MODEL if set, otherwise the application default):")
    print(shlex.join(command))
    print("Check the host repository before and after Apply (fails before, passes after):")
    print(shlex.join(["git", "-C", str(repo), "diff"]))
    print(shlex.join(["uv", "run", "--directory", str(repo), "--no-project", "python", "-B", "-m", "unittest", "-v"]))


if __name__ == "__main__":
    main()
