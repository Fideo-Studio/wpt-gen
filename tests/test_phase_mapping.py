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

"""Tests for the web features mapping phase."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from wptgen.metadata import current_features
from wptgen.models import WorkflowContext, WorkflowError
from wptgen.phases.mapping import REPORT_FILENAME, run_web_features_mapping
from wptgen.web_features_manifest import (
    ResolvedManifest,
    WebFeaturesManifestError,
    WPTCheckout,
)


def _write(root: Path, rel: str, text: str = "") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest(root: Path, dest: Path) -> Path:
    """Stands in for `./wpt web-features-manifest` on a small tree."""
    data: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.html")):
        rel = path.relative_to(root)
        if "-ref" not in rel.name:
            for feature in current_features(root, rel):
                data.setdefault(feature, []).append(f"/{rel.as_posix()}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"version": 1, "data": data}))
    return dest


@pytest.fixture
def wpt_root(tmp_path: Path) -> Path:
    root = tmp_path / "wpt"
    _write(root, "css/WEB_FEATURES.yml", 'rules:\n- "**": [css]\n')
    _write(root, "css/grid/old.html")
    _write(root, "p/WEB_FEATURES.yml", 'rules:\n- "*": [popover]\n')
    _write(root, "p/existing.html")
    return root


@pytest.fixture
def config(wpt_root: Path) -> MagicMock:
    return MagicMock(wpt_path=str(wpt_root), yes_tests=False)


@pytest.fixture
def ui() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def wpt_tools(mocker: MockerFixture) -> dict[str, MagicMock]:
    return {
        "classify": mocker.patch(
            "wptgen.phases.mapping.classify_test_files",
            side_effect=lambda _root, paths: {
                p: "support" if "-ref" in p else "testharness" for p in paths
            },
        ),
        "build": mocker.patch(
            "wptgen.phases.mapping.build_manifest_at",
            side_effect=lambda root, dest: _manifest(Path(root), Path(dest)),
        ),
        "fetch": mocker.patch(
            "wptgen.phases.mapping.fetch_feature_yaml", return_value={}
        ),
    }


def _run(
    wpt_root: Path,
    config: MagicMock,
    ui: MagicMock,
    generated: list[str],
    declared: dict[str, list[str]] | None = None,
) -> tuple[Any, Path]:
    state_dir = wpt_root.parent / "state"
    before = _manifest(wpt_root, wpt_root.parent / "before.json")
    for rel in generated:
        if not (wpt_root / rel).exists():
            _write(wpt_root, rel)
    context = WorkflowContext(
        feature_id="grid",
        generated_tests=[(wpt_root / rel, "", "") for rel in generated],
        feature_mappings={
            str(wpt_root / rel): {"feature_ids": ids, "reason": "overlap"}
            for rel, ids in (declared or {}).items()
        },
        web_features_manifest=ResolvedManifest(
            str(before), "now", WPTCheckout(str(wpt_root), "abc", True)
        ),
    )
    state_dir.mkdir(exist_ok=True)
    report = run_web_features_mapping(context, config, ui, state_dir)
    return report, state_dir


def test_target_only_mappings_are_confirmed(
    wpt_root: Path, config: MagicMock, ui: MagicMock
) -> None:
    ui.confirm.return_value = True
    report, state_dir = _run(
        wpt_root, config, ui, ["css/grid/new.html", "css/grid/new-ref.html"]
    )
    assert report.skipped == {"css/grid/new-ref.html": "not a test file"}
    assert (wpt_root / "css/grid/WEB_FEATURES.yml").read_text() == (
        'rules:\n- new.html: [grid]\n- "**": [css]\n'
    )
    assert report.planned[0].review == []
    ui.confirm.assert_called_once()
    assert json.loads((state_dir / REPORT_FILENAME).read_text())["approved"]


def test_judgment_calls_are_listed_and_confirmed(
    wpt_root: Path, config: MagicMock, ui: MagicMock
) -> None:
    ui.confirm.return_value = True
    report, _ = _run(
        wpt_root,
        config,
        ui,
        ["css/grid/new.html", "p/existing.html"],
        declared={"css/grid/new.html": ["grid", "subgrid"]},
    )
    assert [p.review for p in report.planned] == [
        ["also covers subgrid: overlap"],
        ["already mapped to popover; kept"],
    ]
    ui.confirm.assert_called_once()
    assert current_features(wpt_root, Path("css/grid/new.html")) == [
        "grid",
        "subgrid",
    ]
    assert current_features(wpt_root, Path("p/existing.html")) == [
        "grid",
        "popover",
    ]


def test_declined_mappings_are_not_written(
    wpt_root: Path,
    config: MagicMock,
    ui: MagicMock,
    wpt_tools: dict[str, MagicMock],
) -> None:
    ui.confirm.return_value = False
    report, _ = _run(wpt_root, config, ui, ["p/existing.html"])
    assert not report.approved
    assert report.changes == []
    assert current_features(wpt_root, Path("p/existing.html")) == ["popover"]
    wpt_tools["build"].assert_not_called()


def test_yes_tests_skips_the_prompt(
    wpt_root: Path, config: MagicMock, ui: MagicMock
) -> None:
    config.yes_tests = True
    report, _ = _run(wpt_root, config, ui, ["p/existing.html"])
    ui.confirm.assert_not_called()
    assert report.changes[0].feature_ids == ("grid", "popover")


def test_unknown_feature_ids_stop_before_writing(
    wpt_root: Path,
    config: MagicMock,
    ui: MagicMock,
    wpt_tools: dict[str, MagicMock],
) -> None:
    wpt_tools["fetch"].return_value = None
    with pytest.raises(WorkflowError, match="Unknown web feature IDs: made-up"):
        _run(
            wpt_root,
            config,
            ui,
            ["css/grid/new.html"],
            declared={"css/grid/new.html": ["grid", "made-up"]},
        )
    assert not (wpt_root / "css/grid/WEB_FEATURES.yml").exists()


@pytest.mark.parametrize(
    ("tweak", "problem"),
    [
        (
            lambda data: data.pop("grid"),
            "css/grid/new.html is not mapped to grid in the rebuilt manifest",
        ),
        (
            lambda data: data.update(css=[]),
            "/css/grid/old.html is no longer mapped to css",
        ),
    ],
)
def test_rebuilt_manifest_must_agree(
    wpt_root: Path,
    config: MagicMock,
    ui: MagicMock,
    wpt_tools: dict[str, MagicMock],
    tweak: Callable[[dict[str, list[str]]], Any],
    problem: str,
) -> None:
    def build(root: Path, dest: Path) -> Path:
        _manifest(Path(root), Path(dest))
        raw = json.loads(Path(dest).read_text())
        tweak(raw["data"])
        Path(dest).write_text(json.dumps(raw))
        return Path(dest)

    wpt_tools["build"].side_effect = build
    with pytest.raises(WorkflowError, match="doesn't match"):
        _run(wpt_root, config, ui, ["css/grid/new.html"])
    state = wpt_root.parent / "state" / REPORT_FILENAME
    assert json.loads(state.read_text())["problems"] == [problem]


def test_no_rebuild_when_nothing_changes(
    wpt_root: Path,
    config: MagicMock,
    ui: MagicMock,
    wpt_tools: dict[str, MagicMock],
) -> None:
    _write(wpt_root, "css/grid/WEB_FEATURES.yml", 'rules:\n- "*": [grid]\n')
    report, _ = _run(wpt_root, config, ui, ["css/grid/new.html"])
    assert report.after_manifest is None
    wpt_tools["build"].assert_not_called()


def test_tool_errors_stop_the_run(
    wpt_root: Path,
    config: MagicMock,
    ui: MagicMock,
    wpt_tools: dict[str, MagicMock],
) -> None:
    wpt_tools["build"].side_effect = WebFeaturesManifestError("build failed")
    with pytest.raises(WorkflowError, match="build failed"):
        _run(wpt_root, config, ui, ["css/grid/new.html"])


def test_needs_a_feature_and_checkout(
    config: MagicMock, ui: MagicMock, tmp_path: Path
) -> None:
    with pytest.raises(WorkflowError, match="web feature ID"):
        run_web_features_mapping(WorkflowContext(), config, ui, tmp_path)
