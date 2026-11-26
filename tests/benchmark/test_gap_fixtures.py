# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for gap fixture snapshotting and injection."""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from benchmark.gap_fixtures import (
    inject_fixture,
    main,
    normalize_fixture_dir,
    snapshot_fixture,
    validate_fixture_dir,
    validate_manifest_fixtures,
)
from benchmark.gap_manifest import load_gap_manifest
from benchmark.gap_scoring import GapHarnessError, assert_run_is_pinned
from wptgen.config import Config
from wptgen.engine import WPTGenEngine
from wptgen.models import (
    FeatureMetadata,
    WorkflowContext,
    WorkflowPhase,
    WPTContext,
)


def _snapshot_files(root: Path) -> dict[Path, bytes]:
    """Return every file under root keyed by its relative path."""
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command with an explicit environment and timeout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ},
    )
    return result.stdout


@pytest.mark.asyncio
async def test_gap_inject_resumes_at_coverage_audit_without_touching_wpt(
    mock_config: Config,
    mock_ui: MagicMock,
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Injection replays fixtures and only reruns the coverage audit."""
    mocker.patch("wptgen.engine.get_llm_client")
    assert mock_config.wpt_path is not None

    manifest = (
        Path(__file__).resolve().parents[2] / "benchmarks/gap/manifest.yaml"
    )
    pin, canary, _, _ = load_gap_manifest(manifest)

    wpt_dir = Path(mock_config.wpt_path)
    wpt_dir.mkdir()
    (wpt_dir / "test.html").write_text("original", encoding="utf-8")
    _git(["init", "-q"], wpt_dir)
    _git(["config", "user.email", "gap@example.com"], wpt_dir)
    _git(["config", "user.name", "Gap Test"], wpt_dir)
    _git(["add", "test.html"], wpt_dir)
    _git(["commit", "-q", "-m", "init"], wpt_dir)
    git_before = _git(["status", "--porcelain"], wpt_dir)
    wpt_before = _snapshot_files(wpt_dir)

    rep_dir = tmp_path / "gap-run-capture-stream-canvas-1"
    rep_dir.mkdir()
    context = WorkflowContext(
        feature_id="capture-stream-canvas",
        metadata=FeatureMetadata(name="Test", description="Desc", specs=[]),
        spec_contents={"https://example.com/spec": "spec text"},
        wpt_context=WPTContext(
            test_contents={
                "mediacapture-fromelement/idlharness.window.js": "test"
            },
            test_to_deps={
                "mediacapture-fromelement/idlharness.window.js": set()
            },
        ),
        requirements_xml="<reqs/>",
    )
    assert context.wpt_context is not None
    expected_test_contents = context.wpt_context.test_contents
    (rep_dir / "resume_capture-stream-canvas.json").write_text(
        json.dumps(context.to_dict()), encoding="utf-8"
    )
    (rep_dir / "requirements.json").write_text(
        json.dumps({"requirements_xml": "<reqs/>"}), encoding="utf-8"
    )
    (rep_dir / "wpt_checkout.json").write_text(
        json.dumps({"head": pin, "dirty": False}),
        encoding="utf-8",
    )
    (rep_dir / "fixture.json").write_text(
        json.dumps(
            {
                "feature": "capture-stream-canvas",
                "canary": canary,
                "wpt_pin": pin,
                "sha256": {},
            }
        ),
        encoding="utf-8",
    )
    requirements_before = (rep_dir / "requirements.json").read_bytes()
    checkout_before = (rep_dir / "wpt_checkout.json").read_bytes()
    fixture_before = (rep_dir / "fixture.json").read_bytes()

    mock_config.state_dir = str(rep_dir)
    mock_config.resume_from = WorkflowPhase.COVERAGE_AUDIT
    mock_config.suggestions_only = True

    mock_assembly = mocker.patch("wptgen.engine.run_context_assembly")
    mock_extraction = mocker.patch(
        "wptgen.engine.run_requirements_extraction_categorized"
    )
    mock_iterative = mocker.patch(
        "wptgen.engine.run_requirements_extraction_iterative"
    )
    mock_single = mocker.patch("wptgen.engine.run_requirements_extraction")
    mock_audit = mocker.patch(
        "wptgen.engine.run_coverage_audit", return_value="<audit_response/>"
    )
    mock_report = mocker.patch("wptgen.engine.provide_coverage_report")

    engine = WPTGenEngine(config=mock_config, ui=mock_ui)
    result = await engine._run_async_workflow("capture-stream-canvas")

    mock_assembly.assert_not_called()
    mock_extraction.assert_not_called()
    mock_iterative.assert_not_called()
    mock_single.assert_not_called()
    mock_audit.assert_called_once()
    mock_report.assert_awaited_once()
    assert any(
        entry.args[0] == "Explicitly resuming workflow from: coverage_audit"
        for entry in mock_ui.success.call_args_list
    )
    assert result.spec_contents == context.spec_contents
    assert result.wpt_context is not None
    assert result.wpt_context.test_contents == expected_test_contents
    assert (rep_dir / "test_suggestions.json").exists()
    assert (rep_dir / "requirements.json").read_bytes() == requirements_before
    assert (rep_dir / "wpt_checkout.json").read_bytes() == checkout_before
    assert (rep_dir / "fixture.json").read_bytes() == fixture_before
    assert_run_is_pinned(rep_dir, pin, canary)
    assert _snapshot_files(wpt_dir) == wpt_before
    assert _git(["status", "--porcelain"], wpt_dir) == git_before


def _write_gap_manifest(
    path: Path,
    *,
    feature: str,
    fixture_dir: Path,
    pin: str = "ead4350d59e92c6b14484d910da87bc3096635b3",
    canary: str = "test-canary-guid",
) -> Path:
    """Write a minimal gap manifest pointing at a temp fixture directory."""
    path.write_text(
        f"wpt_upstream_commit: {pin}\n"
        f'canary: "{canary}"\n'
        "fixtures:\n"
        f"  {feature}:\n"
        "    reason: js-small-spec\n"
        f"    path: {fixture_dir}\n"
        "gap_corpus: []\n",
        encoding="utf-8",
    )
    return path


def test_snapshot_fixture_freezes_artifacts_and_writes_metadata(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """snapshot_fixture runs pinned generation and records file hashes."""
    feature = "capture-stream-canvas"
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    wpt_pin_dir = tmp_path / "wpt-pin"
    wpt_pin_dir.mkdir()
    fixture_dir = tmp_path / "fixtures" / feature
    tmp_dir = tmp_path / "snapshot-tmp"
    manifest = _write_gap_manifest(
        tmp_path / "manifest.yaml",
        feature=feature,
        fixture_dir=fixture_dir,
        pin=pin,
        canary=canary,
    )

    resume_payload = {
        "feature_id": feature,
        "spec_contents": {"https://example.com/spec": "spec text"},
        "wpt_context": {"test_contents": {"test.html": "body"}},
    }
    resume_bytes = json.dumps(resume_payload).encode("utf-8")
    expected_resume = (json.dumps(resume_payload, indent=2) + "\n").encode(
        "utf-8"
    )
    reqs_bytes = json.dumps([{"id": "R1", "spec_quote": "q"}]).encode("utf-8")
    checkout_bytes = json.dumps({"head": pin, "dirty": False}).encode("utf-8")
    expected_checkout = (
        json.dumps(
            {
                "checkout": {
                    "head": pin,
                    "dirty": False,
                    "dirty_fingerprint": None,
                }
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")

    def fake_run(*_args: Any, **_kwargs: Any) -> MagicMock:
        (tmp_dir / f"resume_{feature}.json").write_bytes(resume_bytes)
        (tmp_dir / "requirements.json").write_bytes(reqs_bytes)
        (tmp_dir / "wpt_checkout.json").write_bytes(checkout_bytes)
        return MagicMock(returncode=0)

    mock_run = mocker.patch(
        "benchmark.gap_fixtures.subprocess.run", side_effect=fake_run
    )

    dest = snapshot_fixture(feature, wpt_pin_dir, manifest, tmp_dir)

    assert dest == fixture_dir
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd == [
        "wpt-gen",
        "generate",
        feature,
        "--wpt-dir",
        str(wpt_pin_dir),
        "--suggestions-only",
        "--state-dir",
        str(tmp_dir),
        "--no-cache",
        "--yes-tokens",
    ]
    assert mock_run.call_args.kwargs["check"] is True
    assert mock_run.call_args.kwargs["timeout"] == 600
    assert isinstance(mock_run.call_args.kwargs["env"], dict)

    assert (dest / f"resume_{feature}.json").read_bytes() == expected_resume
    assert (dest / "requirements.json").read_bytes() == reqs_bytes
    assert (dest / "wpt_checkout.json").read_bytes() == expected_checkout

    meta: Any = json.loads((dest / "fixture.json").read_text(encoding="utf-8"))
    assert meta == {
        "feature": feature,
        "canary": canary,
        "wpt_pin": pin,
        "sha256": {
            f"resume_{feature}.json": hashlib.sha256(
                expected_resume
            ).hexdigest(),
            "requirements.json": hashlib.sha256(reqs_bytes).hexdigest(),
            "wpt_checkout.json": hashlib.sha256(expected_checkout).hexdigest(),
        },
    }
    assert_run_is_pinned(dest, pin, canary)


def test_snapshot_fixture_rejects_unknown_feature(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Snapshotting a feature absent from the manifest fails before running."""
    manifest = _write_gap_manifest(
        tmp_path / "manifest.yaml",
        feature="capture-stream-canvas",
        fixture_dir=tmp_path / "fixtures" / "capture-stream-canvas",
    )
    mock_run = mocker.patch("benchmark.gap_fixtures.subprocess.run")

    with pytest.raises(GapHarnessError, match="feature not listed in manifest"):
        snapshot_fixture(
            "unknown-feature",
            tmp_path / "wpt-pin",
            manifest,
            tmp_path / "snapshot-tmp",
        )

    mock_run.assert_not_called()


def test_snapshot_fixture_rejects_missing_canary(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Snapshotting requires a non-empty canary in the manifest."""
    feature = "capture-stream-canvas"
    manifest = _write_gap_manifest(
        tmp_path / "manifest.yaml",
        feature=feature,
        fixture_dir=tmp_path / "fixtures" / feature,
        canary="",
    )
    mock_run = mocker.patch("benchmark.gap_fixtures.subprocess.run")

    with pytest.raises(GapHarnessError, match="manifest canary is missing"):
        snapshot_fixture(
            feature,
            tmp_path / "wpt-pin",
            manifest,
            tmp_path / "snapshot-tmp",
        )

    mock_run.assert_not_called()


@pytest.mark.parametrize(
    ("head", "dirty"),
    [
        ("wrong-pin", False),
        ("ead4350d59e92c6b14484d910da87bc3096635b3", True),
    ],
)
def test_snapshot_fixture_rejects_unpinned_or_dirty_checkout(
    mocker: MockerFixture,
    tmp_path: Path,
    head: str,
    dirty: bool,
) -> None:
    """Dirty or off-pin snapshots fail before writing to the fixture dir."""
    feature = "capture-stream-canvas"
    fixture_dir = tmp_path / "fixtures" / feature
    tmp_dir = tmp_path / "snapshot-tmp"
    manifest = _write_gap_manifest(
        tmp_path / "manifest.yaml",
        feature=feature,
        fixture_dir=fixture_dir,
    )

    def fake_run(*_args: Any, **_kwargs: Any) -> MagicMock:
        (tmp_dir / f"resume_{feature}.json").write_text(
            json.dumps(
                {
                    "spec_contents": {"https://example.com/spec": "text"},
                    "wpt_context": {"test_contents": {"test.html": "body"}},
                }
            ),
            encoding="utf-8",
        )
        (tmp_dir / "requirements.json").write_text("[]", encoding="utf-8")
        (tmp_dir / "wpt_checkout.json").write_text(
            json.dumps({"head": head, "dirty": dirty}), encoding="utf-8"
        )
        return MagicMock(returncode=0)

    mocker.patch("benchmark.gap_fixtures.subprocess.run", side_effect=fake_run)

    with pytest.raises(
        GapHarnessError, match="wpt_checkout_head_equals_pin_and_clean"
    ):
        snapshot_fixture(feature, tmp_path / "wpt-pin", manifest, tmp_dir)

    assert not fixture_dir.exists()


def test_snapshot_fixture_rejects_empty_test_batch_before_writing_dest(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Empty wpt_context.test_contents fails without creating the fixture dir."""
    feature = "capture-stream-canvas"
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    fixture_dir = tmp_path / "fixtures" / feature
    tmp_dir = tmp_path / "snapshot-tmp"
    manifest = _write_gap_manifest(
        tmp_path / "manifest.yaml",
        feature=feature,
        fixture_dir=fixture_dir,
        pin=pin,
    )

    def fake_run(*_args: Any, **_kwargs: Any) -> MagicMock:
        (tmp_dir / f"resume_{feature}.json").write_text(
            json.dumps(
                {
                    "spec_contents": {"https://example.com/spec": "text"},
                    "wpt_context": {"test_contents": {}},
                }
            ),
            encoding="utf-8",
        )
        (tmp_dir / "requirements.json").write_text("[]", encoding="utf-8")
        (tmp_dir / "wpt_checkout.json").write_text(
            json.dumps({"head": pin, "dirty": False}), encoding="utf-8"
        )
        return MagicMock(returncode=0)

    mocker.patch("benchmark.gap_fixtures.subprocess.run", side_effect=fake_run)

    with pytest.raises(
        GapHarnessError, match="wpt_context.test_contents is empty"
    ):
        snapshot_fixture(feature, tmp_path / "wpt-pin", manifest, tmp_dir)

    assert not fixture_dir.exists()


def test_snapshot_fixture_relativizes_paths_and_strips_manifest(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """Snapshots store WPT-relative paths and no machine-specific metadata."""
    feature = "capture-stream-canvas"
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    wpt_pin_dir = (tmp_path / "wpt-pin").resolve()
    wpt_pin_dir.mkdir()
    fixture_dir = tmp_path / "fixtures" / feature
    tmp_dir = tmp_path / "snapshot-tmp"
    manifest = _write_gap_manifest(
        tmp_path / "manifest.yaml",
        feature=feature,
        fixture_dir=fixture_dir,
        pin=pin,
        canary=canary,
    )

    absolute_test = str(wpt_pin_dir / "tests" / "a.html")
    absolute_dep = str(wpt_pin_dir / "resources" / "dep.js")
    resume = {
        "feature_id": feature,
        "spec_contents": {"https://example.com/spec": "spec text"},
        "wpt_context": {
            "test_contents": {absolute_test: "body"},
            "dependency_contents": {absolute_dep: "js"},
            "test_to_deps": {absolute_test: [absolute_dep]},
        },
        "web_features_manifest": {
            "path": "/Users/someone/cache.json",
            "created_at": "2026-01-01T00:00:00+00:00",
            "checkout": {
                "root": str(wpt_pin_dir),
                "head": pin,
                "dirty": False,
            },
        },
    }
    checkout = {
        "path": "/Users/someone/cache.json",
        "created_at": "2026-01-01T00:00:00+00:00",
        "checkout": {
            "root": str(wpt_pin_dir),
            "head": pin,
            "dirty": False,
            "dirty_fingerprint": None,
        },
    }

    def fake_run(*_args: Any, **_kwargs: Any) -> MagicMock:
        (tmp_dir / f"resume_{feature}.json").write_text(
            json.dumps(resume), encoding="utf-8"
        )
        (tmp_dir / "requirements.json").write_text("[]", encoding="utf-8")
        (tmp_dir / "wpt_checkout.json").write_text(
            json.dumps(checkout), encoding="utf-8"
        )
        return MagicMock(returncode=0)

    mocker.patch("benchmark.gap_fixtures.subprocess.run", side_effect=fake_run)

    dest = snapshot_fixture(feature, wpt_pin_dir, manifest, tmp_dir)

    stored_resume: Any = json.loads(
        (dest / f"resume_{feature}.json").read_text(encoding="utf-8")
    )
    context = stored_resume["wpt_context"]
    assert context["test_contents"] == {"tests/a.html": "body"}
    assert context["dependency_contents"] == {"resources/dep.js": "js"}
    assert context["test_to_deps"] == {"tests/a.html": ["resources/dep.js"]}
    assert "web_features_manifest" not in stored_resume

    stored_checkout: Any = json.loads(
        (dest / "wpt_checkout.json").read_text(encoding="utf-8")
    )
    assert stored_checkout == {
        "checkout": {
            "head": pin,
            "dirty": False,
            "dirty_fingerprint": None,
        }
    }
    assert str(wpt_pin_dir) not in (dest / f"resume_{feature}.json").read_text(
        encoding="utf-8"
    )
    assert str(wpt_pin_dir) not in (dest / "wpt_checkout.json").read_text(
        encoding="utf-8"
    )
    validate_fixture_dir(dest, feature, pin, canary)


def _write_valid_fixture_dir(
    fixture_dir: Path,
    *,
    feature: str = "capture-stream-canvas",
    pin: str = "ead4350d59e92c6b14484d910da87bc3096635b3",
    canary: str = "test-canary-guid",
    test_contents: dict[str, str] | None = None,
    spec_contents: dict[str, str] | None = None,
    resume_extra: dict[str, Any] | None = None,
    checkout: dict[str, Any] | None = None,
) -> Path:
    """Create a complete synthetic frozen fixture directory."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    resume = {
        "feature_id": feature,
        "spec_contents": (
            {"https://example.com/spec": "spec text"}
            if spec_contents is None
            else spec_contents
        ),
        "wpt_context": {
            "test_contents": (
                {"test.html": "body"}
                if test_contents is None
                else test_contents
            )
        },
    }
    if resume_extra is not None:
        resume.update(resume_extra)
    checkout_payload = checkout or {
        "checkout": {
            "head": pin,
            "dirty": False,
            "dirty_fingerprint": None,
        }
    }
    files = {
        f"resume_{feature}.json": json.dumps(resume).encode("utf-8"),
        "requirements.json": json.dumps([{"id": "R1"}]).encode("utf-8"),
        "wpt_checkout.json": json.dumps(checkout_payload).encode("utf-8"),
    }
    hashes: dict[str, str] = {}
    for name, raw in files.items():
        (fixture_dir / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    (fixture_dir / "fixture.json").write_text(
        json.dumps(
            {
                "feature": feature,
                "canary": canary,
                "wpt_pin": pin,
                "sha256": hashes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return fixture_dir


def test_validate_fixture_dir_accepts_valid_fixture(tmp_path: Path) -> None:
    """A valid frozen fixture passes provenance, hash, and payload checks."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture", pin=pin, canary=canary
    )

    validate_fixture_dir(fixture_dir, "capture-stream-canvas", pin, canary)


def test_validate_fixture_dir_rejects_bad_sha256(tmp_path: Path) -> None:
    """Tampering with a frozen file fails the SHA-256 check."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture", pin=pin, canary=canary
    )
    (fixture_dir / "requirements.json").write_text("[]", encoding="utf-8")

    with pytest.raises(GapHarnessError, match="sha256 mismatch"):
        validate_fixture_dir(fixture_dir, "capture-stream-canvas", pin, canary)


def test_validate_fixture_dir_rejects_empty_test_contents(
    tmp_path: Path,
) -> None:
    """A frozen fixture must have a non-empty wpt_context.test_contents."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture", pin=pin, canary=canary, test_contents={}
    )

    with pytest.raises(
        GapHarnessError, match="wpt_context.test_contents is empty"
    ):
        validate_fixture_dir(fixture_dir, "capture-stream-canvas", pin, canary)


def test_validate_fixture_dir_rejects_stray_test_suggestions(
    tmp_path: Path,
) -> None:
    """Audit output must not be committed inside a frozen fixture."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture", pin=pin, canary=canary
    )
    (fixture_dir / "test_suggestions.json").write_text(
        "<test_suggestion/>", encoding="utf-8"
    )

    with pytest.raises(GapHarnessError, match="test_suggestions.json"):
        validate_fixture_dir(fixture_dir, "capture-stream-canvas", pin, canary)


def test_validate_fixture_dir_rejects_absolute_paths(
    tmp_path: Path,
) -> None:
    """A committed fixture must not store host-absolute WPT paths."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture",
        pin=pin,
        canary=canary,
        test_contents={"/Users/someone/wpt/tests/a.html": "body"},
    )

    with pytest.raises(GapHarnessError, match="non-relative WPT paths"):
        validate_fixture_dir(fixture_dir, "capture-stream-canvas", pin, canary)


def test_validate_fixture_dir_rejects_embedded_manifest(
    tmp_path: Path,
) -> None:
    """A committed fixture must not embed the author's cached manifest."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture",
        pin=pin,
        canary=canary,
        resume_extra={
            "web_features_manifest": {
                "path": "/Users/someone/cache.json",
                "created_at": "2026-01-01T00:00:00+00:00",
                "checkout": {
                    "root": "/Users/someone/wpt",
                    "head": pin,
                    "dirty": False,
                },
            }
        },
    )

    with pytest.raises(GapHarnessError, match="web_features_manifest"):
        validate_fixture_dir(fixture_dir, "capture-stream-canvas", pin, canary)


def test_validate_fixture_dir_rejects_untrimmed_checkout(
    tmp_path: Path,
) -> None:
    """A committed fixture's provenance must not carry host-specific fields."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture",
        pin=pin,
        canary=canary,
        checkout={
            "checkout": {
                "root": "/Users/someone/wpt",
                "head": pin,
                "dirty": False,
            }
        },
    )

    with pytest.raises(GapHarnessError, match="unexpected fields"):
        validate_fixture_dir(fixture_dir, "capture-stream-canvas", pin, canary)


def test_inject_fixture_reroots_paths(tmp_path: Path) -> None:
    """Injection copies a fixture into a fresh dir and re-roots its paths."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    feature = "capture-stream-canvas"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture",
        pin=pin,
        canary=canary,
        resume_extra={
            "wpt_context": {
                "test_contents": {"tests/a.html": "body"},
                "dependency_contents": {"resources/dep.js": "js"},
                "test_to_deps": {"tests/a.html": ["resources/dep.js"]},
            }
        },
    )
    wpt_pin_dir = (tmp_path / "wpt-pin").resolve()
    wpt_pin_dir.mkdir()
    rep_dir = tmp_path / "runs" / "rep-1"

    inject_fixture(
        feature,
        fixture_dir,
        wpt_pin_dir,
        rep_dir,
        pin=pin,
        canary=canary,
    )

    resume: Any = json.loads(
        (rep_dir / f"resume_{feature}.json").read_text(encoding="utf-8")
    )
    context = resume["wpt_context"]
    absolute_test = str((wpt_pin_dir / "tests" / "a.html").resolve())
    absolute_dep = str((wpt_pin_dir / "resources" / "dep.js").resolve())
    assert context["test_contents"] == {absolute_test: "body"}
    assert context["dependency_contents"] == {absolute_dep: "js"}
    assert context["test_to_deps"] == {absolute_test: [absolute_dep]}
    assert (rep_dir / "requirements.json").is_file()
    assert (rep_dir / "fixture.json").is_file()
    assert_run_is_pinned(rep_dir, pin, canary)


def test_inject_fixture_rejects_existing_rep_dir(tmp_path: Path) -> None:
    """Injection never reuses a repeat directory."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    feature = "capture-stream-canvas"
    fixture_dir = _write_valid_fixture_dir(
        tmp_path / "fixture", pin=pin, canary=canary
    )
    rep_dir = tmp_path / "rep-1"
    rep_dir.mkdir()

    with pytest.raises(GapHarnessError, match="already exists"):
        inject_fixture(
            feature,
            fixture_dir,
            tmp_path / "wpt-pin",
            rep_dir,
            pin=pin,
            canary=canary,
        )


def _write_legacy_fixture_dir(
    fixture_dir: Path,
    *,
    feature: str = "capture-stream-canvas",
    pin: str = "ead4350d59e92c6b14484d910da87bc3096635b3",
    canary: str = "test-canary-guid",
) -> tuple[Path, Path]:
    """Create a pre-normalization fixture with host paths and metadata."""
    wpt_root = (fixture_dir.parent / "wpt-pin").resolve()
    wpt_root.mkdir(parents=True, exist_ok=True)
    absolute_test = str(wpt_root / "tests" / "a.html")
    absolute_dep = str(wpt_root / "resources" / "dep.js")
    resume = {
        "feature_id": feature,
        "spec_contents": {"https://example.com/spec": "spec text"},
        "wpt_context": {
            "test_contents": {absolute_test: "body"},
            "dependency_contents": {absolute_dep: "js"},
            "test_to_deps": {absolute_test: [absolute_dep]},
        },
        "web_features_manifest": {
            "path": "/Users/someone/cache.json",
            "created_at": "2026-01-01T00:00:00+00:00",
            "checkout": {
                "root": str(wpt_root),
                "head": pin,
                "dirty": False,
            },
        },
    }
    checkout = {
        "path": "/Users/someone/cache.json",
        "created_at": "2026-01-01T00:00:00+00:00",
        "checkout": {
            "root": str(wpt_root),
            "head": pin,
            "dirty": False,
            "dirty_fingerprint": None,
        },
    }
    fixture_dir.mkdir(parents=True, exist_ok=True)
    files = {
        f"resume_{feature}.json": json.dumps(resume).encode("utf-8"),
        "requirements.json": json.dumps([{"id": "R1"}]).encode("utf-8"),
        "wpt_checkout.json": json.dumps(checkout).encode("utf-8"),
    }
    hashes: dict[str, str] = {}
    for name, raw in files.items():
        (fixture_dir / name).write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    (fixture_dir / "fixture.json").write_text(
        json.dumps(
            {
                "feature": feature,
                "canary": canary,
                "wpt_pin": pin,
                "sha256": hashes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return fixture_dir, wpt_root


def test_normalize_fixture_dir_rewrites_legacy_fixture(tmp_path: Path) -> None:
    """Normalization relativizes paths, trims provenance, and rehashes."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    feature = "capture-stream-canvas"
    fixture_dir, wpt_root = _write_legacy_fixture_dir(
        tmp_path / "fixtures" / feature,
        feature=feature,
        pin=pin,
        canary=canary,
    )

    normalize_fixture_dir(
        fixture_dir,
        feature,
        pin=pin,
        canary=canary,
        wpt_root=wpt_root,
    )

    validate_fixture_dir(fixture_dir, feature, pin, canary)
    resume: Any = json.loads(
        (fixture_dir / f"resume_{feature}.json").read_text(encoding="utf-8")
    )
    assert resume["wpt_context"]["test_contents"] == {"tests/a.html": "body"}
    assert "web_features_manifest" not in resume
    checkout: Any = json.loads(
        (fixture_dir / "wpt_checkout.json").read_text(encoding="utf-8")
    )
    assert checkout == {
        "checkout": {
            "head": pin,
            "dirty": False,
            "dirty_fingerprint": None,
        }
    }
    assert str(wpt_root) not in (
        fixture_dir / f"resume_{feature}.json"
    ).read_text(encoding="utf-8")


def test_normalize_fixture_dir_rejects_mismatched_pin(
    tmp_path: Path,
) -> None:
    """Normalization must not silently rewrite a fixture from another pin."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    feature = "capture-stream-canvas"
    fixture_dir, wpt_root = _write_legacy_fixture_dir(
        tmp_path / "fixtures" / feature,
        feature=feature,
        pin=pin,
        canary=canary,
    )

    with pytest.raises(GapHarnessError, match="wpt_pin mismatch"):
        normalize_fixture_dir(
            fixture_dir,
            feature,
            pin="different-pin",
            canary=canary,
            wpt_root=wpt_root,
        )


def test_cli_normalize_rewrites_legacy_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The normalize CLI converts a legacy fixture and then verifies it."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    feature = "capture-stream-canvas"
    fixture_dir, _ = _write_legacy_fixture_dir(
        tmp_path / "fixtures" / feature,
        feature=feature,
        pin=pin,
        canary=canary,
    )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"wpt_upstream_commit: {pin}\n"
        f'canary: "{canary}"\n'
        "fixtures:\n"
        f"  {feature}:\n"
        "    reason: js-small-spec\n"
        f"    path: {fixture_dir}\n"
        "gap_corpus: []\n",
        encoding="utf-8",
    )

    assert main(["normalize", "--manifest", str(manifest)]) == 0
    assert main(["verify", "--manifest", str(manifest)]) == 0
    captured = capsys.readouterr()
    assert f"Normalized fixture: {fixture_dir}" in captured.out
    assert "Validated 1 gap fixture(s)" in captured.out


def test_validate_manifest_fixtures_handles_missing_entries(
    tmp_path: Path,
) -> None:
    """require_all=True rejects missing dirs; require_all=False skips them."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    present_dir = _write_valid_fixture_dir(
        tmp_path / "fixtures" / "capture-stream-canvas",
        feature="capture-stream-canvas",
        pin=pin,
        canary=canary,
    )
    missing_dir = tmp_path / "fixtures" / "offline-audio-context"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"wpt_upstream_commit: {pin}\n"
        f'canary: "{canary}"\n'
        "fixtures:\n"
        "  capture-stream-canvas:\n"
        "    reason: js-small-spec\n"
        f"    path: {present_dir}\n"
        "  offline-audio-context:\n"
        "    reason: js-section-of-huge-spec\n"
        f"    path: {missing_dir}\n"
        "gap_corpus: []\n",
        encoding="utf-8",
    )

    with pytest.raises(GapHarnessError, match="missing fixture directory"):
        validate_manifest_fixtures(manifest, require_all=True)

    assert validate_manifest_fixtures(manifest, require_all=False) == [
        present_dir
    ]


def test_committed_manifest_fixtures_are_valid() -> None:
    """Every committed fixture in benchmarks/gap/manifest.yaml is valid."""
    repo_root = Path(__file__).resolve().parents[2]
    manifest = repo_root / "benchmarks" / "gap" / "manifest.yaml"

    validated = validate_manifest_fixtures(
        manifest, repo_root=repo_root, require_all=False
    )

    assert (
        repo_root / "benchmarks" / "gap" / "fixtures" / "capture-stream-canvas"
    ) in validated


def test_cli_snapshot_single_and_all_missing(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    """The snapshot CLI handles one feature and --all-missing."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    existing = _write_valid_fixture_dir(
        tmp_path / "fixtures" / "capture-stream-canvas",
        feature="capture-stream-canvas",
        pin=pin,
        canary=canary,
    )
    missing = tmp_path / "fixtures" / "offline-audio-context"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"wpt_upstream_commit: {pin}\n"
        f'canary: "{canary}"\n'
        "fixtures:\n"
        "  capture-stream-canvas:\n"
        "    reason: js-small-spec\n"
        f"    path: {existing}\n"
        "  offline-audio-context:\n"
        "    reason: js-section-of-huge-spec\n"
        f"    path: {missing}\n"
        "gap_corpus: []\n",
        encoding="utf-8",
    )
    wpt_dir = tmp_path / "wpt-pin"
    wpt_dir.mkdir()
    config_file = tmp_path / "wpt-gen.yml"
    config_file.write_text("version: '1'\n", encoding="utf-8")

    mock_snap = mocker.patch(
        "benchmark.gap_fixtures.snapshot_fixture", return_value=missing
    )

    assert (
        main(
            [
                "snapshot",
                "offline-audio-context",
                "--wpt-dir",
                str(wpt_dir),
                "--manifest",
                str(manifest),
                "--tmp-dir",
                str(tmp_path / "run-tmp"),
                "--config",
                str(config_file),
            ]
        )
        == 0
    )
    assert mock_snap.call_args.args[:4] == (
        "offline-audio-context",
        wpt_dir,
        manifest,
        tmp_path / "run-tmp",
    )
    assert mock_snap.call_args.kwargs["config_path"] == config_file

    mock_snap.reset_mock()
    assert (
        main(
            [
                "snapshot",
                "--all-missing",
                "--wpt-dir",
                str(wpt_dir),
                "--manifest",
                str(manifest),
            ]
        )
        == 0
    )
    assert [call.args[0] for call in mock_snap.call_args_list] == [
        "offline-audio-context"
    ]

    assert main(["snapshot", "--wpt-dir", str(wpt_dir)]) == 1


def test_cli_verify_exit_codes(tmp_path: Path) -> None:
    """The verify CLI exits 0 when valid and 1 when a fixture is missing."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    present = _write_valid_fixture_dir(
        tmp_path / "fixtures" / "capture-stream-canvas",
        feature="capture-stream-canvas",
        pin=pin,
        canary=canary,
    )
    missing = tmp_path / "fixtures" / "offline-audio-context"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"wpt_upstream_commit: {pin}\n"
        f'canary: "{canary}"\n'
        "fixtures:\n"
        "  capture-stream-canvas:\n"
        "    reason: js-small-spec\n"
        f"    path: {present}\n"
        "  offline-audio-context:\n"
        "    reason: js-section-of-huge-spec\n"
        f"    path: {missing}\n"
        "gap_corpus: []\n",
        encoding="utf-8",
    )

    assert main(["verify", "--manifest", str(manifest)]) == 1
    assert main(["verify", "--manifest", str(manifest), "--allow-missing"]) == 0


def test_cli_snapshot_all_missing_continues_past_failures(
    mocker: MockerFixture,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """--all-missing finishes remaining features and reports failures at end."""
    pin = "ead4350d59e92c6b14484d910da87bc3096635b3"
    canary = "test-canary-guid"
    first_dir = tmp_path / "fixtures" / "bad-feature"
    second_dir = tmp_path / "fixtures" / "share"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"wpt_upstream_commit: {pin}\n"
        f'canary: "{canary}"\n'
        "fixtures:\n"
        "  bad-feature:\n"
        "    reason: broken\n"
        f"    path: {first_dir}\n"
        "  share:\n"
        "    reason: js-small-spec-error-cases\n"
        f"    path: {second_dir}\n"
        "gap_corpus: []\n",
        encoding="utf-8",
    )
    wpt_dir = tmp_path / "wpt-pin"
    wpt_dir.mkdir()

    def fake_snapshot(feature: str, *_args: Any, **_kwargs: Any) -> Path:
        if feature == "bad-feature":
            raise GapHarnessError("fixture wpt_context.test_contents is empty")
        return second_dir

    mock_snap = mocker.patch(
        "benchmark.gap_fixtures.snapshot_fixture", side_effect=fake_snapshot
    )

    rc = main(
        [
            "snapshot",
            "--all-missing",
            "--wpt-dir",
            str(wpt_dir),
            "--manifest",
            str(manifest),
        ]
    )

    assert rc == 1
    assert [call.args[0] for call in mock_snap.call_args_list] == [
        "bad-feature",
        "share",
    ]
    captured = capsys.readouterr()
    assert f"Wrote fixture: {second_dir}" in captured.out
    assert "Snapshot failed for 1 feature(s): bad-feature" in captured.err
