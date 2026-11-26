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

"""Tests for coverage_audit.py."""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from jinja2 import Environment
from pytest_mock import MockerFixture

from wptgen.config import Config
from wptgen.models import WorkflowContext, WorkflowError, WPTContext
from wptgen.phases.coverage_audit import (
    check_worksheet_rows,
    combine_audit_responses,
    partition_requirements_xml,
    provide_coverage_report,
    run_coverage_audit,
)


@pytest.fixture
def mock_ui(mocker: MockerFixture) -> Any:
    ui = mocker.MagicMock()
    ui.status.return_value.__enter__.return_value = None
    return ui


@pytest.fixture
def mock_config(tmp_path: Path) -> Config:
    return Config(
        provider="test",
        default_model="test-model",
        api_key="test-key",
        wpt_path=str(tmp_path),
        categories={},
        phase_model_mapping={},
    )


@pytest.fixture
def mock_llm(mocker: MockerFixture) -> Any:
    llm = mocker.MagicMock()
    llm.prompt_exceeds_input_token_limit.return_value = False
    return llm


@pytest.fixture
def mock_jinja_env(mocker: MockerFixture) -> Any:
    env = mocker.MagicMock(spec=Environment)
    mock_template = mocker.MagicMock()
    mock_template.render.return_value = "Rendered Prompt"
    env.get_template.return_value = mock_template
    return env


@pytest.mark.asyncio
async def test_run_coverage_audit_token_limit_exceeded(
    mock_config: Config,
    mock_ui: MagicMock,
    mock_llm: MagicMock,
    mock_jinja_env: MagicMock,
) -> None:
    context = WorkflowContext(
        feature_id="test",
        requirements_xml=(
            '<requirements><requirement id="R1">Test</requirement>'
            "</requirements>"
        ),
        wpt_context=WPTContext(),
    )

    mock_llm.prompt_exceeds_input_token_limit.return_value = True

    result = await run_coverage_audit(
        context, mock_config, mock_llm, mock_ui, mock_jinja_env
    )

    assert result is None
    mock_ui.error.assert_called_once_with(
        "This test suite to too large to audit."
    )


def test_combine_audit_responses_all_satisfied() -> None:
    responses = [
        "<status>SATISFIED</status>\n<audit_worksheet>W1</audit_worksheet>",
        "<status>SATISFIED</status>\n<audit_worksheet>W2</audit_worksheet>",
    ]
    result = combine_audit_responses(responses)
    assert "<status>SATISFIED</status>" in result
    assert "<audit_worksheet>\nW1\nW2\n</audit_worksheet>" in result
    assert "test_suggestions" not in result


def test_combine_audit_responses_with_suggestions() -> None:
    responses = [
        "<status>SATISFIED</status>\n<audit_worksheet>W1</audit_worksheet>",
        "<audit_worksheet>W2</audit_worksheet>\n<test_suggestions>\n"
        "<test_suggestion>T1</test_suggestion>\n</test_suggestions>",
    ]
    result = combine_audit_responses(responses)
    assert "<status>TESTS_NEEDED</status>" in result
    assert "<audit_worksheet>\nW1\nW2\n</audit_worksheet>" in result
    assert (
        "<test_suggestions>\n<test_suggestion>T1</test_suggestion>\n"
        "</test_suggestions>" in result
    )


@pytest.mark.asyncio
async def test_run_coverage_audit_single_partition(
    mocker: MockerFixture,
    mock_config: Config,
    mock_ui: MagicMock,
    mock_llm: MagicMock,
    mock_jinja_env: MagicMock,
) -> None:
    context = WorkflowContext(
        feature_id="test",
        requirements_xml=(
            '<requirements><requirement id="R1">Test</requirement>'
            "</requirements>"
        ),
        wpt_context=WPTContext(),
    )

    mocker.patch("wptgen.phases.coverage_audit.confirm_prompts")
    mocker.patch(
        "wptgen.phases.coverage_audit.generate_safe",
        return_value=(
            "<status>SATISFIED</status>\n"
            "<audit_worksheet>R1: Test -> [COVERED by a.html]</audit_worksheet>"
        ),
    )

    result = await run_coverage_audit(
        context, mock_config, mock_llm, mock_ui, mock_jinja_env
    )

    assert result is not None
    assert "<status>SATISFIED</status>" in result


@pytest.mark.asyncio
async def test_run_coverage_audit_multiple_partitions(
    mocker: MockerFixture,
    mock_config: Config,
    mock_ui: MagicMock,
    mock_llm: MagicMock,
    mock_jinja_env: MagicMock,
) -> None:
    reqs = "".join(
        [f'<requirement id="R{i}">T</requirement>' for i in range(50)]
    )
    context = WorkflowContext(
        feature_id="test",
        requirements_xml=f"<requirements>{reqs}</requirements>",
        wpt_context=WPTContext(),
    )

    mocker.patch("wptgen.phases.coverage_audit.confirm_prompts")
    mocker.patch(
        "wptgen.phases.coverage_audit.generate_safe",
        side_effect=[_worksheet(range(25)), _worksheet(range(25, 50))],
    )

    result = await run_coverage_audit(
        context, mock_config, mock_llm, mock_ui, mock_jinja_env
    )

    assert result is not None
    assert "R24: T -> [UNCOVERED]\nR25: T -> [UNCOVERED]" in result


def _worksheet(ids: range) -> str:
    rows = "\n".join(f"R{i}: T -> [UNCOVERED]" for i in ids)
    return f"<audit_worksheet>\n{rows}\n</audit_worksheet>"


@pytest.mark.asyncio
async def test_run_coverage_audit_fails_on_a_failed_partition(
    mocker: MockerFixture,
    mock_config: Config,
    mock_ui: MagicMock,
    mock_llm: MagicMock,
    mock_jinja_env: MagicMock,
) -> None:
    reqs = "".join(f'<requirement id="R{i}">T</requirement>' for i in range(50))
    context = WorkflowContext(feature_id="test", requirements_xml=reqs)
    mocker.patch("wptgen.phases.coverage_audit.confirm_prompts")
    mocker.patch(
        "wptgen.phases.coverage_audit.generate_safe",
        side_effect=[_worksheet(range(25)), WorkflowError("timeout")],
    )

    with pytest.raises(WorkflowError, match="1 of 2 coverage audit"):
        await run_coverage_audit(
            context, mock_config, mock_llm, mock_ui, mock_jinja_env
        )
    assert context.audit_response is None


@pytest.mark.parametrize(
    ("worksheet", "message"),
    [
        ("R1: a -> [UNCOVERED]\nR3 is covered", "no row for R2, R3"),
        (
            "R1: a -> [UNCOVERED]\nR2: b -> [UNCOVERED]\n"
            "R3: c -> [UNCOVERED]\nR1: a -> [COVERED by x.html]",
            "more than one row for R1",
        ),
    ],
)
def test_check_worksheet_rows(worksheet: str, message: str) -> None:
    reqs = "".join(f'<requirement id="R{i}">T</requirement>' for i in (1, 2, 3))
    with pytest.raises(WorkflowError, match=message):
        check_worksheet_rows(
            reqs, f"<audit_worksheet>{worksheet}</audit_worksheet>"
        )


@pytest.mark.asyncio
async def test_provide_coverage_report_success(
    mock_config: Config, mock_ui: MagicMock, tmp_path: Path
) -> None:
    mock_config.output_dir = str(tmp_path)
    context = WorkflowContext(
        feature_id="test", audit_response="Mock audit response"
    )

    await provide_coverage_report(context, mock_config, mock_ui)
    mock_ui.report_coverage_audit.assert_called_with("Mock audit response")
    mock_ui.warning.assert_called_once_with(
        "No existing tests were audited, so every requirement is reported "
        "as uncovered."
    )


def test_partition_requirements_xml_empty() -> None:
    assert not partition_requirements_xml("")


def test_partition_requirements_xml_no_matches() -> None:
    assert partition_requirements_xml("<foo></foo>") == ["<foo></foo>"]
    assert not partition_requirements_xml("   ")


@pytest.mark.asyncio
async def test_run_coverage_audit_always_brief_suggestions(
    mocker: MockerFixture,
    mock_config: Config,
    mock_ui: MagicMock,
    mock_llm: MagicMock,
    mock_jinja_env: MagicMock,
) -> None:
    context = WorkflowContext(
        feature_id="test",
        requirements_xml=(
            '<requirements><requirement id="R1">Test</requirement>'
            "</requirements>"
        ),
        wpt_context=WPTContext(),
    )

    mock_config.brief_suggestions = True

    audit_template_mock = mocker.MagicMock()
    audit_template_mock.render.return_value = "Audit Prompt"

    system_template_mock = mocker.MagicMock()
    system_template_mock.render.return_value = "System Prompt"

    def mock_get_template(name: str) -> Any:
        if name == "coverage_audit.jinja":
            return audit_template_mock
        if name == "coverage_audit_system.jinja":
            return system_template_mock
        return mocker.MagicMock()

    mock_jinja_env.get_template.side_effect = mock_get_template

    mocker.patch("wptgen.phases.coverage_audit.confirm_prompts")
    mocker.patch(
        "wptgen.phases.coverage_audit.generate_safe",
        return_value="<audit_worksheet>R1: Test -> [UNCOVERED]</audit_worksheet>",
    )

    await run_coverage_audit(
        context, mock_config, mock_llm, mock_ui, mock_jinja_env
    )

    system_template_mock.render.assert_called_once_with(
        brief_suggestions=True, spec_urls=[]
    )
