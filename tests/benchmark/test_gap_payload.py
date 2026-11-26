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

"""Tests for the shared gap payload typed dict."""

from typing import is_typeddict

from benchmark.gap_payload import GapPayload
from benchmark.gap_scoring import mechanical_issues
from wptgen.phases.report_render import RequirementAudit


def _payload() -> GapPayload:
    """Build a valid scoring payload using dict-key syntax."""
    payload: GapPayload = {
        "rows": [
            RequirementAudit(
                id="R1",
                category="C",
                text="first requirement",
                status="UNCOVERED",
            ),
            RequirementAudit(
                id="R2",
                category="C",
                text="second requirement",
                status="COVERED",
                tests=["test.html"],
            ),
        ],
        "suggestions_by_requirement": {"R1": ["add a test"]},
        "batch_paths": {"test.html"},
        "spec_quotes": ["quoted spec text"],
        "spec_quotes_by_requirement": {"R1": "quoted spec text"},
    }
    return payload


def test_gap_payload_is_a_typed_dict_with_the_shared_keys() -> None:
    """The scorer and harness share one dictionary contract."""
    assert is_typeddict(GapPayload)
    assert set(GapPayload.__annotations__) == {
        "rows",
        "suggestions_by_requirement",
        "batch_paths",
        "spec_quotes",
        "spec_quotes_by_requirement",
    }


def test_gap_payload_round_trips_through_the_scorer() -> None:
    """Mechanical checks read the payload with plain dictionary-key access."""
    payload = _payload()

    assert (
        mechanical_issues(
            payload, {"R1", "R2"}, ["quoted spec text"], "quoted spec text"
        )
        == []
    )
