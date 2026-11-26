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

"""Tests for gap worksheet and suggestion parsing."""

import pytest

from benchmark.gap_scoring import (
    GapHarnessError,
    assert_partition_rows_preserved,
    parse_audit_partition,
)
from wptgen.phases.report_render import (
    parse_audit_worksheet,
    parse_test_suggestions,
)
from wptgen.utils import extract_xml_tag, locate_snippet, parse_suggestions


def test_parse_audit_worksheet_joins_tests_to_requirement_ids() -> None:
    """Worksheet parsing keeps IDs, categories, statuses, and cited paths."""
    worksheet = (
        "[Conformance]\n"
        "R1: first requirement -> [COVERED by test-one.html, test-two.html]\n"
        "R2: second requirement -> [UNCOVERED]\n"
    )

    rows = parse_audit_worksheet(worksheet)

    assert [row.id for row in rows] == ["R1", "R2"]
    assert [row.category for row in rows] == ["Conformance", "Conformance"]
    assert rows[0].status == "COVERED"
    assert rows[0].tests == ["test-one.html", "test-two.html"]
    assert rows[1].status == "UNCOVERED"
    assert rows[1].tests == []


def test_parse_test_suggestions_extracts_description_and_steps() -> None:
    """Structured suggestion XML parses into typed suggestion data."""
    suggestions_xml = (
        "<test_suggestion>"
        "<description>Add a parsing test</description>"
        "<title>Parsing test</title>"
        "<steps>"
        "<step>Load the page</step>"
        "<step>Check the result</step>"
        "</steps>"
        "</test_suggestion>"
    )

    suggestions = parse_test_suggestions(suggestions_xml)

    assert len(suggestions) == 1
    assert suggestions[0].description == "Add a parsing test"
    assert suggestions[0].title == "Parsing test"
    assert suggestions[0].steps == ["Load the page", "Check the result"]


def test_parse_suggestions_and_extract_xml_tag() -> None:
    """Raw suggestion blocks expose requirement IDs and spec quotes."""
    raw = (
        "<test_suggestion>"
        "<requirement_id>R1</requirement_id>"
        "<spec_quote>quoted spec text</spec_quote>"
        "</test_suggestion>"
    )

    blocks = parse_suggestions(raw)

    assert len(blocks) == 1
    assert extract_xml_tag(blocks[0], "requirement_id") == "R1"
    assert extract_xml_tag(blocks[0], "spec_quote") == "quoted spec text"
    assert extract_xml_tag(blocks[0], "description") is None


def test_locate_snippet_finds_reflowed_spec_text() -> None:
    """Citation checks match spec text after whitespace normalization."""
    source = "First line.\n  The   quoted   text.  \nLast line.\n"

    assert locate_snippet("The quoted text.", source) == [
        (2, "  The   quoted   text.  ")
    ]


def test_empty_audit_partition_is_satisfied_then_rejected_by_join() -> None:
    """An empty partition is explicit SATISFIED, never a silent no-gap run."""
    rows = parse_audit_partition("  \n\n")

    assert len(rows) == 1
    assert rows[0].status == "SATISFIED"

    with pytest.raises(
        GapHarnessError, match="partitioned_audit_loses_no_rows"
    ):
        assert_partition_rows_preserved({"R1"}, rows)


def test_non_empty_audit_partition_uses_worksheet_parser() -> None:
    """A populated partition parses through the normal worksheet parser."""
    rows = parse_audit_partition("[C]\nR1: first requirement -> [UNCOVERED]\n")

    assert [row.id for row in rows] == ["R1"]
    assert rows[0].status == "UNCOVERED"
