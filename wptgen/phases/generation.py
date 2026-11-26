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

"""Phase 4: Generation - Interactive test selection and agentic generation."""

import posixpath
import re
from collections import Counter
from pathlib import Path

from jinja2 import Environment
from rich.rule import Rule

from wptgen.config import Config
from wptgen.llm import LLMClient
from wptgen.models import (
    FeatureMetadata,
    TestType,
    WorkflowContext,
    WorkflowPhase,
)
from wptgen.phases.context_assembly import resolve_web_features_manifest
from wptgen.phases.report_render import parse_audit_worksheet
from wptgen.ui import UIProvider
from wptgen.utils import (
    extract_xml_tag,
    get_next_available_root,
    parse_suggestions,
)

# Existing tests passed to each generator agent as examples to start from.
MAX_RELATED_TESTS = 3


async def run_test_generation(
    context: WorkflowContext,
    config: Config,
    llm: LLMClient,
    ui: UIProvider,
    jinja_env: Environment,
) -> list[tuple[Path, str, str]]:
    """Executes the Generation phase.

    Displays test suggestions to the user, gathers approvals, and launches
    the ADK generation agent for each selected test.

    Args:
      context: The current workflow context.
      config: The tool configuration.
      llm: The LLM client.
      ui: The UI provider.
      jinja_env: The Jinja2 environment.

    Returns:
      A list of (path, content, suggestion_xml) tuples for generated tests.
    """
    ui.on_phase_start(
        4,
        "User Selection & Generation",
        model_info=config.get_model_info_for_phase(WorkflowPhase.GENERATION),
    )

    assert context.audit_response is not None
    assert context.metadata is not None

    # Check for satisfaction status
    status = extract_xml_tag(context.audit_response, "status")
    if status and status.strip() == "SATISFIED":
        ui.success("All identified test requirements have been satisfied.")
        ui.info(
            "No new test suggestions were generated because existing coverage "
            "is sufficient."
        )
        return []

    # Display the audit worksheet in a formatted table
    audit_worksheet = extract_xml_tag(context.audit_response, "audit_worksheet")
    if audit_worksheet:
        ui.report_audit_worksheet(audit_worksheet)

    suggestions = parse_suggestions(context.audit_response)

    if not suggestions:
        ui.warning(
            "No valid <test_suggestion> blocks found in the LLM response."
        )
        return []

    ui.success(f"{len(suggestions)} new test suggestions found!\n")

    approved_suggestions_xml = []
    for i, suggestion in enumerate(suggestions):
        title = extract_xml_tag(suggestion, "title") or f"Suggestion #{i + 1}"
        description = (
            extract_xml_tag(suggestion, "description")
            or "No description provided."
        )
        test_type = extract_xml_tag(suggestion, "test_type")

        ui.report_test_suggestion(i + 1, title, description, test_type)
        if config.yes_tests or ui.confirm("Generate this test?"):
            approved_suggestions_xml.append(suggestion)

    if not approved_suggestions_xml:
        ui.warning("No tests selected. Exiting.")
        return []

    return await _generate_adk_loop(
        approved_suggestions_xml, context, config, ui, jinja_env
    )


async def run_single_test_generation(
    feature_id: str | None,
    spec_urls: list[str],
    description: str,
    title: str | None,
    test_type: str | None,
    config: Config,
    ui: UIProvider,
    jinja_env: Environment,
) -> list[tuple[Path, str, str]]:
    """Generates a single test directly from a user description."""
    ui.on_phase_start(4, "Single Test Generation")

    context = WorkflowContext(feature_id=feature_id)
    context.metadata = FeatureMetadata(
        name=feature_id or "custom_feature", description="", specs=spec_urls
    )

    title_xml = f"  <title>{title}</title>\n" if title else ""
    type_xml = f"  <test_type>{test_type}</test_type>\n" if test_type else ""

    suggestion_xml = f"""<test_suggestion>
{title_xml}  <description>{description}</description>
{type_xml}</test_suggestion>"""

    return await _generate_adk_loop(
        [suggestion_xml], context, config, ui, jinja_env
    )


def _audit_hints(
    suggestion_xml: str, context: WorkflowContext, wpt_root: Path
) -> tuple[list[str], str | None]:
    """Picks existing tests and a directory from the audit for a suggestion.

    Args:
      suggestion_xml: The suggestion XML from the audit response.
      context: The workflow context, with the audit and the audited tests.
      wpt_root: The WPT checkout the audited test paths are under.

    Returns:
      Up to MAX_RELATED_TESTS repository paths of tests the audit cited as
      covering other requirements (those in the suggestion's category first,
      most cited first), and the directory that holds most of the audited
      tests, or None if no tests were audited.
    """
    tests = []
    for path in (
        context.wpt_context.test_contents if context.wpt_context else []
    ):
        try:
            tests.append(Path(path).relative_to(wpt_root).as_posix())
        except ValueError:
            tests.append(path.lstrip("/"))
    if not tests:
        return [], None
    directories = Counter(posixpath.dirname(test) for test in tests)
    target_directory = directories.most_common(1)[0][0]

    # The audit copies each requirement's description into its suggestion.
    description = " ".join(
        (extract_xml_tag(suggestion_xml, "description") or "").split()
    )
    categories: dict[str, str | None] = {}
    category = None
    for block in re.findall(
        r"(?s)<requirement\b[^>]*>.*?</requirement>",
        context.requirements_xml or "",
    ):
        req_id = re.search(r'\bid="([^"]+)"', block)
        if not req_id:
            continue
        categories[req_id.group(1)] = extract_xml_tag(block, "category")
        req_description = extract_xml_tag(block, "description") or ""
        if description and " ".join(req_description.split()) == description:
            category = categories[req_id.group(1)]

    def resolve(cited: str) -> str | None:
        cited = cited.strip().strip("`").lstrip("/")
        if cited in tests:
            return cited
        matches = [test for test in tests if test.endswith(f"/{cited}")]
        return matches[0] if len(matches) == 1 else None

    same_category: Counter[str] = Counter()
    other: Counter[str] = Counter()
    worksheet = extract_xml_tag(context.audit_response or "", "audit_worksheet")
    for row in parse_audit_worksheet(worksheet or ""):
        if row.status != "COVERED":
            continue
        row_category = categories.get(row.id, row.category)
        counts = (
            same_category if category and row_category == category else other
        )
        for cited in row.tests:
            test = resolve(cited)
            if test:
                counts[test] += 1

    related: list[str] = []
    for counts in (same_category, other):
        for test, _ in counts.most_common():
            if test not in related and len(related) < MAX_RELATED_TESTS:
                related.append(test)
    return related, target_directory


def _format_test_suggestion(
    suggestion_xml: str,
    feature_id: str | None,
    spec_urls: list[str],
    sanitize: bool = False,
    related_tests: list[str] | None = None,
    target_directory: str | None = None,
) -> str:
    """Helper to inject required metadata into a test suggestion XML block.

    Args:
      suggestion_xml: The raw suggestion XML from the audit response.
      feature_id: The ID of the web feature.
      spec_urls: A list of specification URLs.
      sanitize: Whether to strip extra tags and return a brief version.
      related_tests: Existing tests the generator can start from.
      target_directory: The directory most of the feature's tests are in.

    Returns:
      A modified XML string with enriched metadata.
    """
    hints = [
        f"  <related_test>{test}</related_test>" for test in related_tests or []
    ]
    if target_directory:
        hints.append(
            f"  <target_directory>{target_directory}</target_directory>"
        )
    if sanitize:
        description = (
            extract_xml_tag(suggestion_xml, "description")
            or "No description provided."
        )
        lines = ["<test_suggestion>"]
        lines.append(f"  <description>{description}</description>")
        # Keep the spec quote so generated tests stay traceable to the spec.
        spec_quote = extract_xml_tag(suggestion_xml, "spec_quote")
        if spec_quote:
            lines.append(f"  <spec_quote>{spec_quote}</spec_quote>")
        for url in spec_urls:
            lines.append(f"  <spec_url>{url}</spec_url>")
        if feature_id:
            lines.append(f"  <feature_id>{feature_id}</feature_id>")
        lines.extend(hints)
        lines.append("</test_suggestion>")
        return "\n".join(lines)
    else:
        # Just inject spec_urls and feature_id into the existing XML
        lines = []
        for url in spec_urls:
            lines.append(f"  <spec_url>{url}</spec_url>")
        if feature_id:
            lines.append(f"  <feature_id>{feature_id}</feature_id>")
        lines.extend(hints)
        additions = "\n".join(lines)
        if additions:
            return suggestion_xml.replace(
                "</test_suggestion>", f"{additions}\n</test_suggestion>"
            )
        return suggestion_xml


async def _generate_adk_loop(
    approved_suggestions_xml: list[str],
    context: WorkflowContext,
    config: Config,
    ui: UIProvider,
    jinja_env: Environment,
) -> list[tuple[Path, str, str]]:
    """Internal loop that launches ADK agents sequentially for each test.

    Args:
      approved_suggestions_xml: List of approved suggestion XML blocks.
      context: The current workflow context.
      config: The tool configuration.
      ui: The UI provider.
      jinja_env: The Jinja2 environment.

    Returns:
      A flat list of all generated test files.
    """
    from wptgen.agents.adk_test_generator import generate_test_with_adk

    # Runs that skipped context assembly resolve the manifest here, before
    # any agent writes to the checkout.
    if context.feature_id and config.wpt_path:
        manifest = context.web_features_manifest
        if manifest is None or not Path(manifest.path).is_file():
            if manifest is not None:
                ui.warning(
                    "The web-features manifest recorded for this run is no "
                    "longer cached; resolving it again."
                )
            context.web_features_manifest = await resolve_web_features_manifest(
                config, ui
            )

    ui.report_generation_start(len(approved_suggestions_xml))

    spec_urls = (
        context.metadata.specs
        if context.metadata and context.metadata.specs
        else []
    )
    output_dir = Path(config.output_dir or ".")
    used_names: set[str] = set()

    tasks = []

    for suggestion_xml in approved_suggestions_xml:
        related_tests, target_directory = (
            _audit_hints(
                suggestion_xml, context, Path(config.wpt_path).resolve()
            )
            if config.wpt_path
            else ([], None)
        )
        modified_xml = _format_test_suggestion(
            suggestion_xml,
            context.feature_id,
            spec_urls,
            sanitize=config.brief_suggestions,
            related_tests=related_tests,
            target_directory=target_directory,
        )

        raw_test_type = (
            extract_xml_tag(modified_xml, "test_type") or "JavaScript Test"
        )
        test_type_enum = TestType.JAVASCRIPT
        for member in TestType:
            if member.value.lower() == raw_test_type.lower():
                test_type_enum = member
                break

        root_name = get_next_available_root(
            context.feature_id or "custom_feature", output_dir, used_names
        )
        used_names.add(root_name)

        tasks.append(
            generate_test_with_adk(
                modified_xml,
                root_name,
                test_type_enum,
                context,
                config,
                jinja_env,
                ui,
            )
        )

    results = []

    # ADK streams events to the UI directly. We await sequentially to avoid
    # output corruption.
    ui.print("\n[bold cyan]Starting ADK Test Generation...[/bold cyan]")

    for i, (task, suggestion_xml) in enumerate(
        zip(tasks, approved_suggestions_xml, strict=True)
    ):
        msg = f"\n[bold yellow]--- Generating Test {i+1} of {len(tasks)} ---"
        ui.print(f"{msg}[/bold yellow]")
        ui.print(
            Rule(
                "[bold cyan]🤖 WPT-Gen Agent[/bold cyan]",
                style="cyan",
                align="left",
            )
        )
        result = await task
        ui.print()
        ui.print(Rule(style="cyan"))
        results.append(result)
        if not result:
            context.unreported_suggestions.append(
                extract_xml_tag(suggestion_xml, "description")
                or f"Suggestion #{i + 1}"
            )

    final_results = [r for sublist in results for r in sublist]

    ui.report_generation_summary(
        final_results, context.run_records, context.unreported_suggestions
    )

    return final_results
