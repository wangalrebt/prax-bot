from __future__ import annotations

from prax_bot.tools import apply_edit

FILE = (
    "def sliding_window(n, seq):\n"
    '    """doc"""\n'
    "    return zip(*(collections.deque(itertools.islice(it, i + 1), 0) or it\n"
    "               for i, it in enumerate(itertools.tee(seq, n))))\n"
    "\n"
    "\n"
    "no_pad = '__no__pad__'\n"
)


def test_exact_match_still_wins() -> None:
    new, err = apply_edit(FILE, "islice(it, i + 1)", "islice(it, i)")
    assert err is None and "islice(it, i), 0)" in new


def test_indentation_mismatch_on_continuation_line_is_tolerated() -> None:
    # what Nemotron actually sent in the benchmark: no indent on the first line, 16 spaces on the second
    old = "return zip(*(collections.deque(itertools.islice(it, i + 1), 0) or it\n                for i, it in enumerate(itertools.tee(seq, n))))"
    new = "return zip(*(collections.deque(itertools.islice(it, i), 0) or it\n                for i, it in enumerate(itertools.tee(seq, n))))"
    out, err = apply_edit(FILE, old, new)
    assert err is None
    assert "    return zip(*(collections.deque(itertools.islice(it, i), 0) or it\n" in out
    # the unchanged continuation line keeps the file's own 15-space indentation
    assert "\n               for i, it in enumerate(itertools.tee(seq, n))))\n" in out
    assert out.count("\n") == FILE.count("\n")


def test_changed_lines_are_shifted_by_the_file_indent() -> None:
    src = "class A:\n    def f(self):\n        return 1\n"
    out, err = apply_edit(src, "def f(self):\n    return 1", "def f(self):\n    x = 2\n    return x")
    assert err is None and out == "class A:\n    def f(self):\n        x = 2\n        return x\n"


def test_trailing_whitespace_is_tolerated() -> None:
    src = "a = 1   \nb = 2\n"
    out, err = apply_edit(src, "a = 1\nb = 2", "a = 10\nb = 2")
    assert err is None and out == "a = 10\nb = 2\n"


def test_line_number_prefixes_are_stripped() -> None:
    out, err = apply_edit("x = 1\ny = 2\n", "1| x = 1\n2| y = 2", "x = 1\ny = 3")
    assert err is None and out == "x = 1\ny = 3\n"


def test_ambiguous_fuzzy_match_is_refused() -> None:
    src = "  a\n  b\n    a\n    b\n"
    _, err = apply_edit(src, "a\nb", "c")
    assert err is not None and "2 places when ignoring indentation" in err


def test_no_match_reports_the_closest_region() -> None:
    _, err = apply_edit(FILE, "return zip(*(collections.deque(itertools.islice(it, i + 2), 0) or it", "x")
    assert err is not None and "closest region is:" in err and "3| " in err and "islice(it, i + 1)" in err


def test_inserted_lines_follow_the_neighbouring_line_not_the_first_line() -> None:
    # the humanize benchmark failure: the model's copy of the block had 0, 9 and 5 spaces where
    # the file has 4, 8 and 4; the inserted line must land at the file's 8, not at 13
    src = (
        "def naturalsize(value):\n"
        "    if gnu:\n"
        '        suffix = suffixes["gnu"]\n'
        "    elif binary:\n"
        '        suffix = suffixes["binary"]\n'
        "    base = 1024\n"
    )
    old = 'if gnu:\n         suffix = suffixes["gnu"]\n     elif binary:\n         suffix = suffixes["binary"]\n     base = 1024'
    new = 'if gnu:\n         suffix = suffixes["gnu"]\n         base = 1024\n     elif binary:\n         suffix = suffixes["binary"]\n         base = 1024'
    out, err = apply_edit(src, old, new)
    assert err is None
    assert out == (
        "def naturalsize(value):\n"
        "    if gnu:\n"
        '        suffix = suffixes["gnu"]\n'
        "        base = 1024\n"
        "    elif binary:\n"
        '        suffix = suffixes["binary"]\n'
        "        base = 1024\n"
    )
    compile(out, "<edited>", "exec")  # it must still be valid Python
