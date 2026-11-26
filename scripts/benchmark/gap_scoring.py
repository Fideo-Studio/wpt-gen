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
"""Gap scoring mechanical checks

Every entry, every repeat: an errored run is an error, never an empty result.
Fail on bad fixture provenance, a missing/duplicated requirement ID, a missing
suggestion, an empty batch, lost partition rows, or an unlocatable spec_quote
(locate_snippet in wptgen/utils.py).

Report per-requirement verdict flip rates across corpus repeats. Record model
and provider settings in every report.

"""

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.gap_payload import GapPayload
from wptgen.phases.report_render import RequirementAudit, parse_audit_worksheet
from wptgen.utils import locate_snippet

UNMAPPED_SUGGESTIONS_KEY = "__unmapped__"

MECHANICAL_CHECKS = (
    "errored_run_is_error",
    "wpt_checkout_head_equals_pin_and_clean",
    "every_input_requirement_appears_once",
    "one_suggestion_per_uncovered",
    "injected_batch_non_empty",
    "partitioned_audit_loses_no_rows",
    "every_spec_quote_located",
)


@dataclass(frozen=True)
class GapScoreSummary:
    verdict_flip_rate: float | None = None  # report only
    mechanical_failures: int | None = None  # gates CI
    model: str = ""
    provider: str = ""


class GapHarnessError(RuntimeError):
    """Raised when a gap run fails a mechanical check."""


def assert_run_not_errored(run_dir: Path) -> None:
    """Reject runs that failed instead of producing audit output."""
    error_file = run_dir / "error.json"
    if error_file.exists():
        data: Any = json.loads(error_file.read_text(encoding="utf-8"))
        if data.get("failed") or data.get("error"):
            raise GapHarnessError("errored_run_is_error")
    suggestions = run_dir / "test_suggestions.json"
    if (
        not suggestions.exists()
        or not suggestions.read_text(encoding="utf-8").strip()
    ):
        raise GapHarnessError("errored_run_is_error: missing audit output")


def assert_run_is_pinned(
    run_dir: Path, pin: str, canary: str | None = None
) -> None:
    """Validate the WPT pin, clean state, and fixture canary of a run."""
    checkout_file = run_dir / "wpt_checkout.json"
    data: Any = json.loads(checkout_file.read_text(encoding="utf-8"))
    checkout: Any = data.get("checkout", data)
    head = checkout.get("head", checkout.get("HEAD"))
    dirty = checkout.get("dirty", False)
    if head != pin or dirty:
        raise GapHarnessError("wpt_checkout_head_equals_pin_and_clean")
    if canary is None:
        return
    fixture_file = run_dir / "fixture.json"
    if not fixture_file.exists():
        raise GapHarnessError("fixture.json missing")
    fixture: Any = json.loads(fixture_file.read_text(encoding="utf-8"))
    if fixture.get("canary") != canary:
        raise GapHarnessError("fixture canary does not match manifest")


def parse_audit_partition(text: str) -> list[RequirementAudit]:
    """Parse one partition; empty output becomes an explicit SATISFIED row."""
    if not text.strip():
        return [
            RequirementAudit(id="", category="", text="", status="SATISFIED")
        ]
    return parse_audit_worksheet(text)


def assert_partition_rows_preserved(
    input_ids: set[str], rows: list[RequirementAudit]
) -> None:
    """Reject a partition join that lost or duplicated requirement rows."""
    counts = Counter(row.id for row in rows)
    missing = sorted(input_ids - counts.keys())
    duplicates = sorted(rid for rid, n in counts.items() if n != 1)
    if missing or duplicates:
        raise GapHarnessError(
            "partitioned_audit_loses_no_rows: "
            f"missing={missing}, duplicated={duplicates}"
        )


def mechanical_issues(
    payload: GapPayload, input_ids: set[str], quotes: list[str], snapshot: str
) -> list[str]:
    issues: list[str] = []
    counts = Counter(row.id for row in payload["rows"])
    suggestions = payload["suggestions_by_requirement"]
    issues += [
        f"input {rid} absent from worksheet"
        for rid in sorted(input_ids - counts.keys())
    ]
    issues += [
        f"{rid} appears {n} times" for rid, n in counts.items() if n != 1
    ]
    issues += [
        f"unmapped test suggestion: {description}"
        for description in suggestions.get(UNMAPPED_SUGGESTIONS_KEY, [])
    ]
    issues += [
        f"suggestion for unknown requirement {rid}"
        for rid in suggestions
        if rid not in input_ids and rid != UNMAPPED_SUGGESTIONS_KEY
    ]
    issues += [
        f"{rid} has {len(descriptions)} suggestions"
        for rid, descriptions in suggestions.items()
        if rid in input_ids and len(descriptions) != 1
    ]
    issues += [
        f"no suggestion for {row.id}"
        for row in payload["rows"]
        if row.status == "UNCOVERED" and not suggestions.get(row.id)
    ]
    if not payload["batch_paths"]:
        issues.append("injected test batch is empty")
    issues += [
        f"spec_quote not located: {quote}"
        for quote in quotes
        if not locate_snippet(quote, snapshot)
    ]
    return issues


def score_verdict_flip_rate(
    payloads: dict[str, GapPayload], input_ids: set[str]
) -> float:
    """Fraction of frozen requirement IDs whose verdict changes by repeat."""
    verdicts_by_id: dict[str, set[str]] = {
        req_id: set() for req_id in input_ids
    }
    for payload in payloads.values():
        seen: set[str] = set()
        for row in payload["rows"]:
            if row.id in verdicts_by_id:
                verdicts_by_id[row.id].add(row.status)
                seen.add(row.id)
        for req_id in input_ids - seen:
            verdicts_by_id[req_id].add("ABSENT")
    if not verdicts_by_id:
        return 0.0
    flipped = sum(len(verdicts) > 1 for verdicts in verdicts_by_id.values())
    return flipped / len(verdicts_by_id)


def check_run_mechanics(
    run_dir: Path,
    payload: GapPayload,
    input_ids: set[str],
    quotes: list[str],
    snapshot: str,
    pin: str,
    canary: str | None = None,
    partition_texts: list[str] | None = None,
) -> list[str]:
    """Run all seven mechanical checks for one (entry, repeat) directory.

    Returns one string per failed check. An empty result means the repeat is
    mechanically sound; callers must count the failures and fail the exit code.
    """
    issues: list[str] = []
    try:
        assert_run_not_errored(run_dir)
    except GapHarnessError as exc:
        issues.append(str(exc))
    try:
        assert_run_is_pinned(run_dir, pin, canary)
    except GapHarnessError as exc:
        issues.append(str(exc))

    if partition_texts is not None:
        joined_rows = [
            row
            for text in partition_texts
            for row in parse_audit_partition(text)
        ]
        try:
            assert_partition_rows_preserved(input_ids, joined_rows)
        except GapHarnessError as exc:
            issues.append(str(exc))

    issues.extend(mechanical_issues(payload, input_ids, quotes, snapshot))
    return issues


def summarize_flip_rates(
    repeats_by_entry: dict[str, dict[str, GapPayload]],
    frozen_ids_by_entry: dict[str, set[str]],
    kind_by_entry: dict[str, str],
) -> dict[str, Any]:
    """Compute report-only verdict flip rates per entry and per feature type."""
    by_entry = {
        entry_id: score_verdict_flip_rate(
            repeat_payloads, frozen_ids_by_entry.get(entry_id, set())
        )
        for entry_id, repeat_payloads in repeats_by_entry.items()
    }
    grouped: dict[str, list[float]] = {}
    for entry_id, rate in by_entry.items():
        grouped.setdefault(kind_by_entry.get(entry_id, "unknown"), []).append(
            rate
        )
    by_kind = {kind: sum(rates) / len(rates) for kind, rates in grouped.items()}
    overall = sum(by_entry.values()) / len(by_entry) if by_entry else None
    return {
        "overall": overall,
        "by_entry": by_entry,
        "by_kind": by_kind,
        "report_only": True,
    }


def resolve_run_model_provider(run_dirs: list[Path]) -> tuple[str, str]:
    """Extract (model, provider) from run metadata; flag mixed runs explicitly."""
    models: set[str] = set()
    providers: set[str] = set()
    for run_dir in run_dirs:
        meta_path = run_dir / "meta.json"
        if not meta_path.exists():
            continue
        try:
            meta: Any = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("model"):
            models.add(str(meta["model"]))
        if meta.get("provider"):
            providers.add(str(meta["provider"]))

    def _collapse(values: set[str]) -> str:
        if not values:
            return ""
        if len(values) == 1:
            return next(iter(values))
        return "MIXED: " + ", ".join(sorted(values))

    return _collapse(models), _collapse(providers)
