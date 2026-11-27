# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Deterministic linter extension for wpt-gen.

This module implements the subset of `layer: deterministic` rules from
`wptgen/skills/wpt-evaluator/references/rules.yaml` that upstream
`wpt lint` does NOT already cover. Each check is named with its
`rules.yaml` id (e.g. `GENERAL-004`) so the linter and the LLM evaluator
share one identifier space.

"""

from __future__ import annotations

import abc
import os
import re
from collections.abc import Callable

# An `Error` is `(rule_id, description, path, line_no)`; `line_no` is None
# for whole-file / filename findings.
Error = tuple[str, str, str, int | None]


# ---------------------------------------------------------------------------
# Filename predicates
#
# Several checks apply only to a subset of tests identifiable by filename
# (e.g. manual tests, `.worker.js` tests). These predicates let both
# line-pattern and path checks gate on the filename without duplicating the
# parsing logic.
# ---------------------------------------------------------------------------


def is_manual_test(path: str) -> bool:
    """A test whose filename marks it manual (`-manual` flag before the ext)."""
    name_stem = os.path.basename(path).split(".", 1)[0]
    return name_stem.endswith("-manual")


def is_worker_js(path: str) -> bool:
    """A `.worker.js` test file."""
    return os.path.basename(path).endswith(".worker.js")


# File-extension groups, mirrored from upstream lint so `file_extensions`
# on a check reads the same way it does there.
EXTENSIONS: dict[str, list[str]] = {
    "html": [".html", ".htm"],
    "xhtml": [".xht", ".xhtml"],
    "svg": [".svg"],
    "js": [".js", ".mjs"],
    "python": [".py"],
}
EXTENSIONS["markup"] = (
    EXTENSIONS["html"] + EXTENSIONS["xhtml"] + EXTENSIONS["svg"]
)
EXTENSIONS["js_all"] = EXTENSIONS["markup"] + EXTENSIONS["js"]


class Regexp(abc.ABC):
    """A line-pattern check. Subclasses set `pattern`, `name`, and
    `description`; `file_extensions` optionally restricts which files it
    applies to (None = all files).

    Mirrors WPT's `tools/lint/rules.py:Regexp`
    """

    pattern: bytes
    name: str
    description: str
    file_extensions: list[str] | None = None

    def __init__(self) -> None:
        self._re: re.Pattern[bytes] = re.compile(self.pattern)

    def applies(self, path: str) -> bool:
        return (
            self.file_extensions is None
            or os.path.splitext(path)[1] in self.file_extensions
        )

    def search(self, line: bytes) -> re.Match[bytes] | None:
        return self._re.search(line)


# ---------------------------------------------------------------------------
# Line-pattern gap rules (self-contained Regexp subclasses)
# ---------------------------------------------------------------------------


class CommentedOutCode(Regexp):
    """CHECKLIST-008: the test must not contain commented-out code."""

    # A `//`-comment whose body looks like code: ends in `;`, `{`, or `}`,
    # or contains a call `foo(`. Deliberately conservative to limit false
    # positives on prose comments.
    pattern = rb"//\s*[A-Za-z_$][\w$.]*\s*\([^)]*\)\s*;?\s*$"
    name = "CHECKLIST-008"
    file_extensions = EXTENSIONS["js_all"]
    description = "Test-file line appears to contain commented-out code"


class ManualExplicitTimeout(Regexp):
    """MANUAL-004: manual testharness tests must pass {explicit_timeout: true}.

    Flags a `setup()` call that lacks `explicit_timeout`, but only in
    manual tests, so ordinary tests' `setup()` calls are not flagged.
    """

    pattern = rb"setup\((?![^)]*explicit_timeout)[^)]*\)"
    name = "MANUAL-004"
    file_extensions = EXTENSIONS["markup"]
    description = (
        "Manual testharness test calls setup() without "
        "{explicit_timeout: true}"
    )

    def applies(self, path: str) -> bool:
        # Only manual tests (`-manual` filename) need explicit_timeout.
        return super().applies(path) and is_manual_test(path)


class DeprecatedCssFlag(Regexp):
    """CSS-METADATA-003: deprecated `<meta name=flags>` tokens for CSS tests.

    The tokens `animated`, `font`, `history`, `interact`, `speech`, and
    `userstyle` are deprecated for new CSS tests; such tests should use the
    `-manual` filename flag instead. Matches a `<meta name=flags ...>`
    element whose `content` includes one of those tokens.
    """

    pattern = (
        rb'<meta\s+name=["\']?flags["\']?\s+content=["\'][^"\']*'
        rb"\b(animated|font|history|interact|speech|userstyle)\b"
    )
    name = "CSS-METADATA-003"
    file_extensions = EXTENSIONS["markup"]
    description = (
        "`<meta name=flags>` uses a token deprecated for new CSS tests "
        "(animated/font/history/interact/speech/userstyle)"
    )


# ---------------------------------------------------------------------------
# Filename gap rules (path logic, no file contents needed)
# ---------------------------------------------------------------------------

# NOTE: GENERAL-004 ("tests requiring HTTPS must carry `.https.`") is NOT
# implemented here. Whether a test *requires* a secure context is a
# semantic property — no fixed regex over API names can decide it
# completely, and scanning full file content for that proxy is both
# noisy and the slowest possible check. GENERAL-004 is `layer: semantic` in
# rules.yaml and left to the LLM judge.


def check_manual_suffix_position(path: str) -> Error | None:
    """FILENAMES-001 / MANUAL-002: `-manual` must be the last `-` element.

    `foo-manual.html` is a manual test; `foo-manual-other.html` is not,
    and if a file carries `-manual` anywhere but immediately before the
    extension it is a likely mistake.
    """
    base = os.path.basename(path)
    stem, _ = os.path.splitext(base)
    if "-manual" in stem and not stem.endswith("-manual"):
        return (
            "FILENAMES-001",
            "`-manual` appears in the filename but is not the last `-` "
            "element before the extension",
            path,
            None,
        )
    return None


def check_crash_suffix_position(path: str) -> Error | None:
    """CRASHTEST-001: `-crash` must be immediately before the extension.

    A file with `-crash` mid-stem (e.g. `bar-crash-001.html`) is NOT a
    crashtest and the misplaced flag is a likely mistake — unless the file
    lives under a `crashtests/` directory, where the flag is not required.
    """
    parts = path.replace("\\", "/").split("/")
    if "crashtests" in parts[:-1]:
        return None
    stem, _ = os.path.splitext(parts[-1])
    if "-crash" in stem and not stem.endswith("-crash"):
        return (
            "CRASHTEST-001",
            "`-crash` appears in the filename but is not immediately before "
            "the extension; such a file is not treated as a crashtest",
            path,
            None,
        )
    return None


def check_print_suffix_position(path: str) -> Error | None:
    """PRINT-REFTESTS-001: `-print` must be immediately before the extension.

    A file with `-print` mid-stem (e.g. `bar-print-001.html`) is NOT a
    print reftest — unless it lives under a `print/` directory, where the
    flag is not required.
    """
    parts = path.replace("\\", "/").split("/")
    if "print" in parts[:-1]:
        return None
    stem, _ = os.path.splitext(parts[-1])
    if "-print" in stem and not stem.endswith("-print"):
        return (
            "PRINT-REFTESTS-001",
            "`-print` appears in the filename but is not immediately before "
            "the extension; such a file is not treated as a print reftest",
            path,
            None,
        )
    return None


def check_multiglobal_extension(path: str) -> Error | None:
    """FILENAMES-005: `.window`, `.worker`, `.any` must be immediately followed
    by the final `.js` extension.

    `foo.any.js` is correct; `foo.any.bar.js` (the token not immediately
    before `.js`) is the mistake this catches.
    """
    base = os.path.basename(path)
    if not base.endswith(".js"):
        return None
    for token in (".window", ".worker", ".any"):
        if token in base and not base.endswith(f"{token}.js"):
            return (
                "FILENAMES-005",
                f"`{token}` should be immediately followed by the final "
                f"`.js` extension",
                path,
                None,
            )
    return None


# ---------------------------------------------------------------------------
# Content gap rules (need full file bytes; gated on filename to stay cheap)
# ---------------------------------------------------------------------------

_IMPORT_TESTHARNESS_RE = re.compile(
    rb"importScripts\(\s*[\"'][^\"']*/resources/testharness\.js[\"']"
)
_DONE_CALL_RE = re.compile(rb"\bdone\s*\(\s*\)")


def check_worker_boilerplate(path: str, content: bytes) -> Error | None:
    """TESTHARNESS-003: a `.worker.js` file must importScripts
    testharness.js and call done().

    Only runs for `.worker.js` files (gated by the driver), so the content
    read is not incurred for other files.
    """
    if not _IMPORT_TESTHARNESS_RE.search(content):
        return (
            "TESTHARNESS-003",
            "`.worker.js` file does not importScripts "
            "`/resources/testharness.js`",
            path,
            None,
        )
    if not _DONE_CALL_RE.search(content):
        return (
            "TESTHARNESS-003",
            "`.worker.js` file does not call `done()`",
            path,
            None,
        )
    return None


_SCRIPT_BLOCK_RE = re.compile(
    rb"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE
)
# A `test()` or `promise_test()` call given a function. `async_test()` only
# declares a test; its assertions come in later steps.
_SUBTEST_CALL_RE = re.compile(
    rb"(?<![\w.$])(?<!function )(?:promise_test|test)\s*\("
    rb"\s*(?:async\b|\(|[\w$]+\s*=>|function\b)"
)
_ASSERTION_RE = re.compile(
    rb"\b(?:assert_\w+|promise_rejects_\w+|unreached_func)\s*\("
)
# A call to a bare function, which may be a helper that asserts.
_BARE_CALL_RE = re.compile(rb"(?<![\w.$])(?<!new )([A-Za-z_$][\w$]*)\s*\(")
_NOT_HELPERS = {
    b"if",
    b"for",
    b"while",
    b"switch",
    b"catch",
    b"function",
    b"return",
    b"typeof",
    b"await",
    b"async",
}
_RETURN_RE = re.compile(rb"\breturn\b")
_STRING_RE = re.compile(rb"""(['"`])((?:\\.|(?!\1).)*)\1""", re.DOTALL)
# Names of smoke tests, which pass unless something throws or crashes.
_NO_THROW_NAME_RE = re.compile(
    rb"(?:not|n't) throw|crash|construct|\bset ?up\b|clean ?up|\bis OK\b",
    re.IGNORECASE,
)


def _without_comments(content: bytes) -> bytes:
    """`content` with comments blanked out, keeping strings and newlines."""
    out = bytearray(content)
    i = 0
    while i < len(content):
        char = content[i : i + 1]
        if char in (b"'", b'"', b"`"):
            i += 1
            while i < len(content) and content[i : i + 1] != char:
                i += 2 if content[i : i + 1] == b"\\" else 1
        elif content.startswith(b"//", i) or content.startswith(b"/*", i):
            end_marker = b"\n" if content[i + 1 : i + 2] == b"/" else b"*/"
            end = content.find(end_marker, i + 2)
            end = len(content) if end == -1 else end
            for j in range(i, end):
                if out[j] != ord("\n"):
                    out[j] = ord(" ")
            i = end
        i += 1
    return bytes(out)


def _call_end(content: bytes, open_paren: int) -> int | None:
    """The index of the parenthesis closing the one at `open_paren`,
    skipping strings and comments, or None if it isn't closed."""
    depth, i = 0, open_paren
    while i < len(content):
        char = content[i : i + 1]
        if char in (b"'", b'"', b"`"):
            i += 1
            while i < len(content) and content[i : i + 1] != char:
                i += 2 if content[i : i + 1] == b"\\" else 1
        elif content.startswith(b"//", i):
            i = content.find(b"\n", i)
            if i == -1:
                return None
        elif content.startswith(b"/*", i):
            i = content.find(b"*/", i)
            if i == -1:
                return None
            i += 1
        elif char == b"(":
            depth += 1
        elif char == b")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def check_subtest_assertions(path: str, content: bytes) -> list[Error]:
    """CHECKLIST-004 (partial): a testharness subtest with no assertions.

    Such a subtest passes unless it throws, rejects or times out, so it
    rarely tests what its name claims. Exempt: subtests named as smoke
    tests ("does not throw", "does not crash", "can be constructed"), those
    that return a value (a promise test's result can depend on it), those
    that call a bare function (which may be a helper that asserts), and
    crash tests. The rest of CHECKLIST-004 stays semantic.
    """
    if "/crashtests/" in path or "-crash." in os.path.basename(path):
        return []
    if path.endswith(tuple(EXTENSIONS["js"])):
        sources = [(0, content)]
    elif b"testharness.js" in content:
        sources = [
            (m.start(1), m.group(1)) for m in _SCRIPT_BLOCK_RE.finditer(content)
        ]
    else:
        return []
    errors: list[Error] = []
    for offset, source in sources:
        source = _without_comments(source)
        for match in _SUBTEST_CALL_RE.finditer(source):
            end = _call_end(source, source.index(b"(", match.start()))
            if end is None:
                continue
            call = source[match.start() : end + 1]
            # The name is the call's last string argument.
            strings = _STRING_RE.findall(call)
            name = strings[-1][1] if strings else b""
            if (
                _ASSERTION_RE.search(call)
                or _NO_THROW_NAME_RE.search(name)
                or _RETURN_RE.search(call)
            ):
                continue
            body = _STRING_RE.sub(b'""', call[call.index(b"(") + 1 :])
            helpers = {m.group(1) for m in _BARE_CALL_RE.finditer(body)}
            if helpers - _NOT_HELPERS:
                continue
            line_no = content.count(b"\n", 0, offset + match.start()) + 1
            errors.append(
                (
                    "CHECKLIST-004",
                    "Subtest has no assertions, so it passes unless it throws, "
                    "rejects or times out; assert what it claims to test",
                    path,
                    line_no,
                )
            )
    return errors


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

# Line-pattern checks (Regexp instances) run against every applicable line.
_LINE_CHECKS: list[Regexp] = [
    CommentedOutCode(),
    ManualExplicitTimeout(),
    DeprecatedCssFlag(),
]

# Path checks run once per file against the path only (no content needed).
_PATH_CHECKS: list[Callable[[str], Error | None]] = [
    check_manual_suffix_position,
    check_crash_suffix_position,
    check_print_suffix_position,
    check_multiglobal_extension,
]

# Content checks need full file bytes. Each is paired with a filename
# predicate so content is only read when at least one applies.
_CONTENT_CHECKS: list[
    tuple[
        Callable[[str], bool],
        Callable[[str, bytes], Error | list[Error] | None],
    ]
] = [
    (is_worker_js, check_worker_boilerplate),
    (
        lambda path: os.path.splitext(path)[1] in EXTENSIONS["js_all"],
        check_subtest_assertions,
    ),
]


def check_file(path: str, content: bytes | None = None) -> list[Error]:
    """Run the gap-set deterministic checks against a single file.

    Args:
        path: Path to the file (used for filename checks and reporting).
        content: File bytes. If omitted, the file is read from disk.

    Returns:
        A list of `Error` tuples `(rule_id, description, path, line_no)`.
    """
    errors: list[Error] = []

    for path_check in _PATH_CHECKS:
        err = path_check(path)
        if err is not None:
            errors.append(err)

    line_checks = [chk for chk in _LINE_CHECKS if chk.applies(path)]
    content_checks = [check for gate, check in _CONTENT_CHECKS if gate(path)]

    # Only read the file if some content-consuming check applies.
    if not line_checks and not content_checks:
        return errors

    if content is None:
        with open(path, "rb") as handle:
            content = handle.read()

    for content_check in content_checks:
        result = content_check(path, content)
        if isinstance(result, tuple):
            errors.append(result)
        elif result:
            errors.extend(result)

    for line_no, line in enumerate(content.splitlines(), start=1):
        for chk in line_checks:
            if chk.search(line):
                errors.append((chk.name, chk.description, path, line_no))

    return errors
