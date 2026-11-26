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

"""Tests for the prompt templating system."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).parent.parent / "wptgen" / "templates"


def test_coverage_audit_system_template_brief_rendering() -> None:
    """Verifies that the coverage audit system templates correctly render brief
    vs. full suggestions.
    """
    template_path = _TEMPLATE_DIR
    env = Environment(loader=FileSystemLoader(str(template_path)))
    template = env.get_template("coverage_audit_system.jinja")

    # Test with brief_suggestions=True
    rendered_brief = template.render(
        brief_suggestions=True, spec_urls=["https://example.com/spec"]
    )
    assert "<title>" not in rendered_brief
    assert "<description>" in rendered_brief
    assert "<spec_quote>" in rendered_brief
    assert "<test_type>" not in rendered_brief
    assert "<pre_conditions>" not in rendered_brief
    assert "<steps>" not in rendered_brief
    assert "<expected_result>" not in rendered_brief
    assert "<spec_url>https://example.com/spec</spec_url>" not in rendered_brief

    # Test with brief_suggestions=False
    rendered_full = template.render(brief_suggestions=False)
    assert "<title>" in rendered_full
    assert "<description>" in rendered_full
    assert "<spec_quote>" in rendered_full
    assert "<test_type>" in rendered_full
    assert "<pre_conditions>" in rendered_full
    assert "<steps>" in rendered_full
    assert "<expected_result>" in rendered_full


def test_requirements_extraction_system_templates_ask_for_spec_quote() -> None:
    """Verifies that every requirements extraction prompt asks for a verbatim
    spec quote on each requirement.
    """
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))
    for name in (
        "requirements_extraction_system.jinja",
        "requirements_extraction_categorized_system.jinja",
        "requirements_extraction_iterative_system.jinja",
    ):
        rendered = env.get_template(name).render(
            category_name="Existence", category_description="D"
        )
        assert "**Verbatim Spec Quote:**" in rendered, name
        assert "<spec_quote>" in rendered, name


def test_adk_test_generator_template_puts_shared_context_first() -> None:
    """The style guide, spec and directory listing come before the
    suggestion, which is the only part that differs between agents."""
    env = Environment(loader=FileSystemLoader(str(_TEMPLATE_DIR)))
    rendered = env.get_template("adk_test_generator.jinja").render(
        suggestion_xml="<test_suggestion>S</test_suggestion>",
        wpt_style_guide="GUIDE",
        spec_contents={"https://spec": "SPEC"},
        target_directory="api",
        directory_listing="a.html",
        output_dir="/wpt",
    )

    positions = [
        rendered.index(text)
        for text in ("GUIDE", "SPEC", '<directory_listing path="api">', "S</")
    ]
    assert positions == sorted(positions)
    assert '<spec_document url="https://spec">' in rendered

    bare = env.get_template("adk_test_generator.jinja").render(
        suggestion_xml="<test_suggestion>S</test_suggestion>"
    )
    assert "<spec_document" not in bare
    assert "<directory_listing" not in bare
