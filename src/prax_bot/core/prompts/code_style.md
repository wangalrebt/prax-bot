Code style:
- Match the surrounding code: its indentation, quote style, naming, import order, line length, and the way it handles errors and logging. The diff should look as if the original author wrote it.
- Keep the change to what the task needs. No drive-by renames, reformatting, reordering or "while I'm here" fixes, and no new dependency unless the task requires one.
- Prefer the simplest code that is correct: no abstraction for a single use, no option for a case that does not exist, no defensive check for a situation the code cannot reach.
- Comments explain why, not what. Leave no debugging prints, commented-out code, TODOs or notes to yourself in the repository.
- Use the project's own tooling: if a formatter, linter or type checker is configured (ruff, black, eslint, prettier, mypy or similar), run it on the files you changed and fix what it reports.
- New tests follow the existing tests' structure, naming and fixtures, and test the behaviour the task describes, including the case that motivated it.
- Preserve public names, signatures and behaviour the task does not ask to change: add, do not break.
