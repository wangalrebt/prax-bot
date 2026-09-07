---
name: pytest-failures
description: How to read a pytest failure and narrow it to one test before editing anything.
---

# Reading pytest failures

1. Run the failing test alone with `-x -q` and the node id (`pytest -x -q tests/test_x.py::test_name`).
2. Read the assertion line and the two frames above it in the traceback; that is usually where the bug is.
3. If the failure mentions a fixture, open `conftest.py` in the same directory before touching the code under test.
4. Reproduce the failure with a five-line script under /tmp/praxbot/scratch when the test setup is heavy.
5. After the fix, run the whole file, not just the one test.
