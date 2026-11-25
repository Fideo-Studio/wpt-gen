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
"""Keeps the evaluator's WPT pin in one place.

The evaluator rules (`rules.yaml`) and the evaluator benchmark
(`benchmarks/manifest.yaml`) are pinned to the same upstream WPT commit, and
Cloud Build checks out that commit by reading the benchmark manifest. These
tests fail if the copies drift apart or a hard-coded pin creeps back in.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "wptgen/skills/wpt-evaluator/references/rules.yaml"
UPSTREAM_PATH = REPO_ROOT / "wptgen/skills/wpt-evaluator/UPSTREAM.md"
MANIFEST_PATH = REPO_ROOT / "benchmarks/manifest.yaml"
CLOUDBUILD_PATH = REPO_ROOT / "cloudbuild.yaml"

FULL_SHA = re.compile(r"\b[0-9a-f]{40}\b")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def test_rules_and_benchmark_share_one_pin() -> None:
    rules_pin = _load(RULES_PATH)["wpt_upstream_commit"]
    manifest_pin = _load(MANIFEST_PATH)["wpt_upstream_commit"]
    assert FULL_SHA.fullmatch(str(rules_pin))
    assert manifest_pin == rules_pin


def test_benchmark_labels_reviewed_against_current_rules() -> None:
    """`rules_version` is the seed-label staleness tripwire."""
    assert str(_load(MANIFEST_PATH)["rules_version"]) == str(
        _load(RULES_PATH)["version"]
    )


def test_rule_ids_are_unique() -> None:
    ids = [rule["id"] for rule in _load(RULES_PATH)["rules"]]
    assert len(ids) == len(set(ids))


def test_no_other_copies_of_the_pin() -> None:
    assert not FULL_SHA.search(UPSTREAM_PATH.read_text())
    assert not FULL_SHA.search(CLOUDBUILD_PATH.read_text())
    assert _load(CLOUDBUILD_PATH)["substitutions"]["_WPT_PIN"] == ""


def _clone_script() -> str:
    """The clone-wpt step's script, with Cloud Build's substitutions applied
    the way Cloud Build applies them (`$$` -> `$`), with a `{wpt_pin}`
    placeholder for the `_WPT_PIN` value."""
    steps = _load(CLOUDBUILD_PATH)["steps"]
    step = next(s for s in steps if s.get("id") == "clone-wpt")
    script = str(step["args"][-1])
    return script.replace("${_WPT_PIN}", "{wpt_pin}").replace("$$", "$")


def _run_clone(
    tmp_path: Path, wpt_pin: str
) -> subprocess.CompletedProcess[str]:
    """Runs the clone step against a fake `git` that logs its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_git = bin_dir / "git"
    fake_git.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path / "git.log"}"\n')
    fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", "-c", _clone_script().replace("{wpt_pin}", wpt_pin)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_cloudbuild_checks_out_the_manifest_pin(tmp_path: Path) -> None:
    result = _run_clone(tmp_path, wpt_pin="")
    assert result.returncode == 0, result.stderr
    pin = _load(MANIFEST_PATH)["wpt_upstream_commit"]
    assert (
        f"-C /workspace/wpt checkout {pin}"
        in (tmp_path / "git.log").read_text()
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_cloudbuild_rejects_a_malformed_pin_override(tmp_path: Path) -> None:
    result = _run_clone(tmp_path, wpt_pin="main")
    assert result.returncode == 1
    assert "Invalid wpt pin" in result.stderr
    assert not (tmp_path / "git.log").exists()
