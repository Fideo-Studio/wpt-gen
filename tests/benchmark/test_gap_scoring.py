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

"""Tests for the gap benchmark mechanical checks."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from benchmark.gap_payload import GapPayload
from benchmark.gap_score_report import write_score
from benchmark.gap_scoring import (
    GapHarnessError,
    GapScoreSummary,
    assert_partition_rows_preserved,
    assert_run_is_pinned,
    assert_run_not_errored,
    mechanical_issues,
    score_verdict_flip_rate,
)
from wptgen.config import Config
from wptgen.phases.report_render import (
    RequirementAudit,
    parse_audit_worksheet,
)
from wptgen.phases.utils import generate_safe
from wptgen.utils import extract_xml_tag, parse_suggestions

PIN = "ead4350d59e92c6b14484d910da87bc3096635b3"
CANARY = "test-canary-guid"
FEATURE = "capture-stream-canvas"


def _write_json(path: Path, data: Any) -> None:
    """Write JSON data to a synthetic run-dir file."""
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _write_synthetic_run_dir(tmp_path: Path) -> Path:
    """Create a valid synthetic gap run dir for single-file mutations."""
    run_dir = tmp_path / "runs" / "gap" / "seed-x" / "rep-0"
    run_dir.mkdir(parents=True, exist_ok=True)

    _write_json(
        run_dir / "requirements.json",
        [
            {"id": "R1", "spec_quote": "quote one"},
            {"id": "R2", "spec_quote": "quote two"},
        ],
    )
    (run_dir / "audit_worksheet.md").write_text(
        "[C]\n"
        "R1: first requirement -> [UNCOVERED]\n"
        "R2: second requirement -> [COVERED by test.html]\n",
        encoding="utf-8",
    )
    (run_dir / "test_suggestions.json").write_text(
        "<test_suggestion>\n"
        "  <requirement_id>R1</requirement_id>\n"
        "  <spec_quote>quote one</spec_quote>\n"
        "  <description>add one test</description>\n"
        "</test_suggestion>\n",
        encoding="utf-8",
    )
    _write_json(
        run_dir / f"resume_{FEATURE}.json",
        {
            "wpt_context": {
                "test_contents": {"test.html": "test body"},
                "test_to_deps": {},
                "dependency_contents": {},
            }
        },
    )
    _write_json(
        run_dir / "wpt_checkout.json",
        {
            "path": "/cache/manifest.json",
            "created_at": "2026-11-25T00:00:00+00:00",
            "checkout": {"root": "/wpt", "head": PIN, "dirty": False},
        },
    )
    _write_json(
        run_dir / "fixture.json",
        {"feature": FEATURE, "canary": CANARY, "wpt_pin": PIN, "sha256": {}},
    )
    (run_dir / "spec_snapshot.txt").write_text(
        "quote one\nquote two\n", encoding="utf-8"
    )
    return run_dir


def _load_inputs(
    run_dir: Path,
) -> tuple[GapPayload, set[str], list[str], str]:
    """Build the scorer inputs from a synthetic run dir."""
    requirements: Any = json.loads(
        (run_dir / "requirements.json").read_text(encoding="utf-8")
    )
    input_ids = {req["id"] for req in requirements}
    quotes = [
        req["spec_quote"] for req in requirements if req.get("spec_quote")
    ]
    snapshot = (run_dir / "spec_snapshot.txt").read_text(encoding="utf-8")

    rows = parse_audit_worksheet(
        (run_dir / "audit_worksheet.md").read_text(encoding="utf-8")
    )
    suggestions_by_requirement: dict[str, list[str]] = {}
    for block in parse_suggestions(
        (run_dir / "test_suggestions.json").read_text(encoding="utf-8")
    ):
        req_id = extract_xml_tag(block, "requirement_id")
        description = extract_xml_tag(block, "description") or ""
        if req_id:
            suggestions_by_requirement.setdefault(req_id, []).append(
                description
            )

    resume: Any = json.loads(
        (run_dir / f"resume_{FEATURE}.json").read_text(encoding="utf-8")
    )
    payload: GapPayload = {
        "rows": rows,
        "suggestions_by_requirement": suggestions_by_requirement,
        "batch_paths": set(resume["wpt_context"]["test_contents"]),
        "spec_quotes": quotes,
        "spec_quotes_by_requirement": {
            req["id"]: req["spec_quote"]
            for req in requirements
            if req.get("spec_quote")
        },
    }
    return payload, input_ids, quotes, snapshot


def _issues(run_dir: Path) -> list[str]:
    """Fire the mechanical checks against a synthetic run dir."""
    payload, input_ids, quotes, snapshot = _load_inputs(run_dir)
    return mechanical_issues(payload, input_ids, quotes, snapshot)


def _row(
    req_id: str, status: str, tests: list[str] | None = None
) -> RequirementAudit:
    """Build a requirement row for scoring assertions."""
    return RequirementAudit(
        id=req_id,
        category="C",
        text=f"text for {req_id}",
        status=status,
        tests=tests or [],
    )


def _scoring_payload(rows: list[RequirementAudit]) -> GapPayload:
    """Build a minimal payload for informational and gate scoring."""
    return {
        "rows": rows,
        "suggestions_by_requirement": {},
        "batch_paths": {"test.html", "withheld.html"},
        "spec_quotes": [],
        "spec_quotes_by_requirement": {},
    }


def test_clean_synthetic_run_has_no_mechanical_issues(tmp_path: Path) -> None:
    """The base synthetic run dir satisfies every payload-level check."""
    assert _issues(_write_synthetic_run_dir(tmp_path)) == []


def test_assert_run_not_errored_accepts_valid_run(tmp_path: Path) -> None:
    """A run with audit output is not treated as errored."""
    assert_run_not_errored(_write_synthetic_run_dir(tmp_path))


def test_assert_run_not_errored_rejects_error_marker(tmp_path: Path) -> None:
    """A failed LLM run is recorded as an error, not an empty gap set."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    _write_json(run_dir / "error.json", {"failed": True, "error": "boom"})

    with pytest.raises(GapHarnessError, match="errored_run_is_error"):
        assert_run_not_errored(run_dir)


@pytest.mark.asyncio
async def test_generate_safe_returns_empty_on_failure(
    mock_config: Config, mock_ui: MagicMock, mock_llm: MagicMock
) -> None:
    """generate_safe() fails loudly by returning an empty response."""
    mock_llm.generate_content.side_effect = RuntimeError("boom")

    result = await generate_safe(
        "prompt", "coverage audit", mock_llm, mock_ui, mock_config
    )

    assert result == ""
    mock_ui.error.assert_called_once()


def test_assert_run_is_pinned_valid_and_invalid(tmp_path: Path) -> None:
    """Provenance checks reject wrong pins, dirty checkouts, and canaries."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    assert_run_is_pinned(run_dir, PIN, CANARY)

    checkout_path = run_dir / "wpt_checkout.json"
    checkout: Any = json.loads(checkout_path.read_text(encoding="utf-8"))
    checkout["checkout"]["head"] = "wrong-pin"
    _write_json(checkout_path, checkout)
    with pytest.raises(
        GapHarnessError, match="wpt_checkout_head_equals_pin_and_clean"
    ):
        assert_run_is_pinned(run_dir, PIN, CANARY)

    checkout["checkout"]["head"] = PIN
    checkout["checkout"]["dirty"] = True
    _write_json(checkout_path, checkout)
    with pytest.raises(
        GapHarnessError, match="wpt_checkout_head_equals_pin_and_clean"
    ):
        assert_run_is_pinned(run_dir, PIN, CANARY)

    checkout["checkout"]["dirty"] = False
    _write_json(checkout_path, checkout)
    _write_json(run_dir / "fixture.json", {"canary": "wrong-canary"})
    with pytest.raises(GapHarnessError, match="canary"):
        assert_run_is_pinned(run_dir, PIN, CANARY)


def test_mechanical_issues_flags_missing_requirement_id(
    tmp_path: Path,
) -> None:
    """A worksheet missing an input requirement ID is rejected."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    (run_dir / "audit_worksheet.md").write_text(
        "[C]\nR1: first requirement -> [UNCOVERED]\n", encoding="utf-8"
    )

    assert "input R2 absent from worksheet" in _issues(run_dir)


def test_mechanical_issues_flags_duplicate_requirement_id(
    tmp_path: Path,
) -> None:
    """A worksheet with a duplicated requirement ID is rejected."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    (run_dir / "audit_worksheet.md").write_text(
        "[C]\n"
        "R1: first requirement -> [COVERED by test.html]\n"
        "R1: duplicate requirement -> [COVERED by test.html]\n"
        "R2: second requirement -> [COVERED by test.html]\n",
        encoding="utf-8",
    )

    assert "R1 appears 2 times" in _issues(run_dir)


def test_mechanical_issues_flags_missing_suggestion(tmp_path: Path) -> None:
    """An UNCOVERED row needs at least one suggestion."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    (run_dir / "test_suggestions.json").write_text("", encoding="utf-8")

    assert "no suggestion for R1" in _issues(run_dir)


def test_mechanical_issues_flags_empty_batch(tmp_path: Path) -> None:
    """An injected batch with no tests is rejected."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    resume_path = run_dir / f"resume_{FEATURE}.json"
    resume: Any = json.loads(resume_path.read_text(encoding="utf-8"))
    resume["wpt_context"]["test_contents"] = {}
    _write_json(resume_path, resume)

    assert "injected test batch is empty" in _issues(run_dir)


def test_partition_join_rejects_lost_rows(tmp_path: Path) -> None:
    """Concatenated audit partitions must retain every input row."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    partitions = [
        "[C]\nR1: first requirement -> [COVERED by test.html]\n",
        "[C]\nR3: third requirement -> [COVERED by test.html]\n",
    ]
    for index, partition in enumerate(partitions):
        (run_dir / f"audit_partition_{index}.md").write_text(
            partition, encoding="utf-8"
        )
    joined = "".join(
        (run_dir / f"audit_partition_{index}.md").read_text(encoding="utf-8")
        for index in range(len(partitions))
    )
    rows = parse_audit_worksheet(joined)

    with pytest.raises(
        GapHarnessError, match="partitioned_audit_loses_no_rows"
    ):
        assert_partition_rows_preserved({"R1", "R2", "R3"}, rows)


def test_mechanical_issues_flags_unlocatable_spec_quote(
    tmp_path: Path,
) -> None:
    """A quote absent from the spec snapshot is rejected."""
    run_dir = _write_synthetic_run_dir(tmp_path)
    requirements_path = run_dir / "requirements.json"
    requirements: Any = json.loads(
        requirements_path.read_text(encoding="utf-8")
    )
    requirements[1]["spec_quote"] = "missing from spec snapshot"
    _write_json(requirements_path, requirements)

    assert "spec_quote not located: missing from spec snapshot" in _issues(
        run_dir
    )


def test_summary_records_model_and_provider(tmp_path: Path) -> None:
    """Every score report records the model and provider settings used."""
    summary = GapScoreSummary(
        verdict_flip_rate=0.0,
        mechanical_failures=0,
        model="test-model",
        provider="test-provider",
    )

    write_score(tmp_path / "score", summary)

    data: Any = json.loads(
        (tmp_path / "score" / "score.json").read_text(encoding="utf-8")
    )
    assert data["model"] == "test-model"
    assert data["provider"] == "test-provider"


def test_verdict_flip_rate_is_keyed_on_frozen_requirement_id() -> None:
    """Flips follow requirement IDs, not row text or unknown IDs."""
    payloads = {
        "rep-0": _scoring_payload(
            [
                _row("R1", "COVERED"),
                _row("R2", "UNCOVERED"),
                _row("R3", "COVERED"),
            ]
        ),
        "rep-1": _scoring_payload(
            [
                _row("R1", "UNCOVERED"),
                _row("R2", "UNCOVERED"),
                _row("R3", "UNCOVERED"),
            ]
        ),
    }

    assert score_verdict_flip_rate(payloads, {"R1", "R2"}) == 0.5


def test_verdict_flip_rate_counts_absent_requirement_as_flip() -> None:
    """A frozen ID missing from one repeat is a verdict change."""
    payloads = {
        "rep-0": _scoring_payload([_row("R1", "COVERED")]),
        "rep-1": _scoring_payload([]),
    }

    assert score_verdict_flip_rate(payloads, {"R1"}) == 1.0


def test_verdict_flip_rate_without_requirements_is_zero() -> None:
    """No frozen requirement IDs means no flips to report."""
    assert score_verdict_flip_rate({}, set()) == 0.0
