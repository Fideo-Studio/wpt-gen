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

"""Tests for WEB_FEATURES.yml reading and writing."""

import json
import subprocess
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from wptgen.metadata import (
    ACTION_ADDED_RULE,
    ACTION_ALREADY_MAPPED,
    ACTION_CREATED_FILE,
    ACTION_UPDATED_RULE,
    WebFeaturesYmlError,
    classify_test_files,
    current_features,
    map_test,
    parse_rules,
    render_rule,
)

POPOVER = (
    "# Popover tests.\n"
    "rules:\n"
    "- popover-close-request.html: [closewatcher]  # keep\n"
    '- "*": [popover]\n'
)


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_rules() -> None:
    rules = parse_rules(
        "rules:\n- a.html: [x]\n- b.html:\n    ids: [y, z]\n- '*': [w]\n"
    )
    assert [(r.pattern, r.feature_ids) for r in rules] == [
        ("a.html", ("x",)),
        ("b.html", ("y", "z")),
        ("*", ("w",)),
    ]
    assert [(r.start_line, r.end_line) for r in rules] == [
        (1, 2),
        (2, 4),
        (4, 5),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("features:\n- name: x\n", "old `features:` format"),
        ("other: 1\n", "no `rules:` list"),
        ("rules: [{a.html: [x]}]\n", "block list"),
        ("rules:\n- a.html: [x]\n  b.html: [y]\n", "exactly one pattern"),
        ("rules:\n- a.html: grid\n", "list of feature IDs"),
        ("rules: [\n", "invalid YAML"),
    ],
)
def test_parse_rules_rejects(text: str, message: str) -> None:
    with pytest.raises(WebFeaturesYmlError, match=message):
        parse_rules(text)


def test_render_rule() -> None:
    assert render_rule("a.html", ["x", "y"]) == "- a.html: [x, y]\n"
    assert render_rule("**", ["x"], "  ") == '  - "**": [x]\n'
    assert render_rule("on.html", ["yes"]) == "- on.html: ['yes']\n"


def test_current_features(tmp_path: Path) -> None:
    _write(tmp_path, "a/WEB_FEATURES.yml", 'rules:\n- "**": [top]\n')
    _write(
        tmp_path,
        "a/b/WEB_FEATURES.yml",
        "rules:\n- x.html: [one]\n- '*.html': [two]\n- '**': [three]\n",
    )
    assert current_features(tmp_path, Path("a/b/x.html")) == ["one"]
    assert current_features(tmp_path, Path("a/b/y.html")) == ["two"]
    assert current_features(tmp_path, Path("a/b/y.js")) == ["three"]
    assert current_features(tmp_path, Path("a/b/c/d.html")) == ["three"]
    assert current_features(tmp_path, Path("a/other/d.html")) == ["top"]
    assert current_features(tmp_path, Path("z/d.html")) == []


def test_map_test_already_mapped(tmp_path: Path) -> None:
    yml = _write(tmp_path, "p/WEB_FEATURES.yml", POPOVER)
    change = map_test(tmp_path, Path("p/new.html"), ["popover"])
    assert change.action == ACTION_ALREADY_MAPPED
    assert yml.read_text() == POPOVER


def test_map_test_inserts_above_the_claiming_rule(tmp_path: Path) -> None:
    yml = _write(tmp_path, "p/WEB_FEATURES.yml", POPOVER)
    change = map_test(tmp_path, Path("p/new.html"), ["dialog", "popover"])
    assert change.action == ACTION_ADDED_RULE
    assert yml.read_text() == (
        "# Popover tests.\n"
        "rules:\n"
        "- popover-close-request.html: [closewatcher]  # keep\n"
        "- new.html: [dialog, popover]\n"
        '- "*": [popover]\n'
    )
    assert current_features(tmp_path, Path("p/other.html")) == ["popover"]


def test_map_test_updates_an_exact_rule(tmp_path: Path) -> None:
    yml = _write(
        tmp_path,
        "d/WEB_FEATURES.yml",
        "rules:\n  - t.html:\n      ids: [a,\n        b]\n  - '*': [x]",
    )
    change = map_test(tmp_path, Path("d/t.html"), ["a", "b", "y"])
    assert change.action == ACTION_UPDATED_RULE
    assert yml.read_text() == "rules:\n  - t.html: [a, b, y]\n  - '*': [x]\n"


def test_map_test_creates_a_file_keeping_inheritance(tmp_path: Path) -> None:
    _write(tmp_path, "css/WEB_FEATURES.yml", 'rules:\n- "**": [css]\n')
    _write(tmp_path, "css/grid/t.html")
    change = map_test(tmp_path, Path("css/grid/t.html"), ["grid"])
    assert change.action == ACTION_CREATED_FILE
    assert (tmp_path / "css/grid/WEB_FEATURES.yml").read_text() == (
        'rules:\n- t.html: [grid]\n- "**": [css]\n'
    )
    assert current_features(tmp_path, Path("css/grid/sub/x.html")) == ["css"]

    _write(tmp_path, "new/t.any.js")
    map_test(tmp_path, Path("new/t.any.js"), ["grid"])
    assert (tmp_path / "new/WEB_FEATURES.yml").read_text() == (
        "rules:\n- t.any.js: [grid]\n"
    )
    assert map_test(tmp_path, Path("css/x/t.html"), ["css"]).action == (
        ACTION_ALREADY_MAPPED
    )


def test_map_test_refuses_files_it_cannot_edit(tmp_path: Path) -> None:
    _write(tmp_path, "old/WEB_FEATURES.yml", "features:\n- name: x\n")
    with pytest.raises(WebFeaturesYmlError, match="old/WEB_FEATURES.yml"):
        map_test(tmp_path, Path("old/t.html"), ["y"])
    _write(tmp_path, "empty/WEB_FEATURES.yml", "rules:\n")
    with pytest.raises(WebFeaturesYmlError):
        map_test(tmp_path, Path("empty/t.html"), ["y"])


def test_classify_test_files(tmp_path: Path, mocker: MockerFixture) -> None:
    assert classify_test_files(tmp_path, []) == {}
    run = mocker.patch(
        "wptgen.metadata.subprocess.run",
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"a.html": "testharness"}), ""
        ),
    )
    assert classify_test_files(tmp_path, ["a.html"]) == {
        "a.html": "testharness"
    }
    assert run.call_args.kwargs["cwd"] == str(tmp_path)

    run.return_value = subprocess.CompletedProcess([], 1, "", "ImportError\n")
    with pytest.raises(WebFeaturesYmlError, match="ImportError"):
        classify_test_files(tmp_path, ["a.html"])
