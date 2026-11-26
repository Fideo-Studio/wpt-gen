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

"""Tests for gap score reporting."""

import json
from pathlib import Path
from typing import Any

from benchmark.gap_score_report import build_report_markdown, write_score
from benchmark.gap_scoring import GapScoreSummary


def _summary() -> GapScoreSummary:
    """Build a representative Step 1 gap score summary."""
    return GapScoreSummary(
        verdict_flip_rate=0.5,
        mechanical_failures=0,
        model="test-model",
        provider="test-provider",
    )


def _absent_summary() -> GapScoreSummary:
    """Build a score summary with no metrics recorded."""
    return GapScoreSummary()


def test_write_score_records_flip_rate(tmp_path: Path) -> None:
    """The machine-readable score includes the reported flip rate."""
    out_dir = tmp_path / "score"

    write_score(out_dir, _summary())

    data = json.loads((out_dir / "score.json").read_text(encoding="utf-8"))
    assert data["verdict_flip_rate"] == 0.5
    assert data["mechanical_failures"] == 0
    assert data["model"] == "test-model"
    assert data["provider"] == "test-provider"


def test_build_report_markdown_shows_checks_and_flip_rates() -> None:
    """The report lists mechanical checks and report-only flip rates."""
    report_json: dict[str, Any] = {
        "summary": {"mechanical_failures": 0, "model": "m", "provider": "p"},
        "flip_rates": {
            "overall": 0.25,
            "by_kind": {"js-small-spec": 0.25},
            "by_entry": {"corpus-example": 0.25},
            "report_only": True,
        },
        "wpt_pin": "pin-value",
        "repeats": 3,
        "mechanical_checks": ["errored_run_is_error"],
    }

    report = build_report_markdown(report_json)

    assert "# Gap Analysis Report" in report
    assert "Failures: 0. These gate the run." in report
    assert "- `errored_run_is_error`" in report
    assert "Report only; never gated." in report
    assert "Overall: 0.25" in report
    assert "| js-small-spec | 0.25 |" in report
    assert "| corpus-example | 0.25 |" in report
    assert "model: m" in report
    assert "provider: p" in report


def test_build_report_markdown_uses_na_for_absent_values() -> None:
    """Absent metrics render as n/a instead of zeros."""
    report = build_report_markdown({"summary": {}, "flip_rates": {}})

    assert "Failures: n/a. These gate the run." in report
    assert "Overall: n/a" in report
    assert "model: n/a" in report
    assert "provider: n/a" in report


def test_absent_summary_defaults_are_reportable() -> None:
    """An unpopulated summary can still be written and rendered."""
    assert _absent_summary().verdict_flip_rate is None
    assert _absent_summary().mechanical_failures is None
