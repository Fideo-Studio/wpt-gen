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

"""Agentic test generation using the Google ADK framework."""

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.skills import load_skill_from_dir
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types
from jinja2 import Environment

from wptgen.agents.provider import create_adk_model, setup_adk_environment
from wptgen.agents.streaming import ADKStreamManager, StreamConfig
from wptgen.agents.tools import (
    RunLog,
    _validate_safe_path,
    create_agent_tools,
    lint_ext_file,
    lint_file,
    run_test_file,
)
from wptgen.config import SKILLS_DIR, Config
from wptgen.metadata import WebFeaturesYmlError, classify_test_files
from wptgen.models import TestType, WorkflowContext
from wptgen.ui import UIProvider
from wptgen.utils import extract_xml_tag

# Specs longer than this (sliced sections rarely are) are left for the agent
# to fetch, rather than sent with every model call.
MAX_PROMPT_SPEC_CHARS = 60_000
MAX_LISTING_ENTRIES = 200


async def generate_test_with_adk(
    suggestion_xml: str,
    root_name: str,
    test_type_enum: TestType,
    context: WorkflowContext,
    config: Config,
    jinja_env: Environment,
    ui: UIProvider,
) -> list[tuple[Path, str, str]]:
    """Runs the ADK Agent to generate tests for a single test suggestion.

    Args:
        suggestion_xml: The XML test suggestion for the test.
        root_name: The base filename to use (e.g., 'feature-1').
        test_type_enum: The type of test to generate.
        context: The workflow context (contains metadata).
        config: The configuration object.
        jinja_env: The Jinja2 environment for loading templates.
        ui: The UI provider for logging output.

    Returns:
        A list of tuples containing (file_path, file_content, suggestion_xml).
    """
    model_string = setup_adk_environment(config)
    if config.provider.lower() == "anthropic" and not model_string.startswith(
        "anthropic/"
    ):
        model_string = f"anthropic/{model_string}"
    elif config.provider.lower() == "openai" and not model_string.startswith(
        "openai/"
    ):
        model_string = f"openai/{model_string}"
    if not config.wpt_path:
        raise ValueError("WPT path is required to generate tests.")
    wpt_root = Path(config.wpt_path)

    # We need to extract the paths from the agent's final tool call.
    generated_paths: list[str] = []
    reported_failures: list[dict[str, Any]] = []
    run_log = RunLog()

    def report_generation_complete(
        file_paths: list[str],
        feature_mappings: list[dict[str, Any]] | None = None,
        known_failures: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Tool to call ONLY when all test files are written to disk.

        Args:
            file_paths: A list of the absolute or relative file paths generated.
            feature_mappings: For each test file (not references or helpers),
                an object with `path`, `feature_ids` (the web feature IDs the
                test covers, normally just the target feature) and `reason`
                (why, if more than one).
            known_failures: For each subtest that still fails and that you
                believe fails because of a browser bug rather than the test,
                an object with `path`, `subtest` and `reason`.

        Returns:
            A dictionary confirming completion.
        """
        generated_paths.extend(file_paths)
        reported_failures.extend(known_failures or [])
        for mapping in feature_mappings or []:
            path = mapping.get("path")
            ids = mapping.get("feature_ids")
            if not isinstance(path, str) or not isinstance(ids, list):
                continue
            try:
                key = str(_validate_safe_path(Path(path), wpt_root))
            except ValueError:
                continue
            context.feature_mappings[key] = {
                "feature_ids": [str(i) for i in ids],
                "reason": str(mapping.get("reason") or ""),
            }
        return {"status": "success", "message": "Generation recorded."}

    tools: list[Any] = list(
        create_agent_tools(
            wpt_root,
            ui,
            config.run_on_browser,
            config.run_on_channel,
            include_run_tool=not config.skip_run,
            omit_search_feature_tests=(context.feature_id is None),
            web_features_manifest_path=(
                context.web_features_manifest.path
                if context.web_features_manifest
                else None
            ),
            run_log=run_log,
        )
    )
    tools.append(FunctionTool(func=report_generation_complete))

    skill_dir = SKILLS_DIR / "wpt-generator"
    if skill_dir.is_dir():
        try:
            wpt_generator_skill = load_skill_from_dir(skill_dir)

            has_feature_id = bool(
                re.search(r"<feature_id>\s*[^<\s]", suggestion_xml)
            )
            template = jinja_env.from_string(wpt_generator_skill.instructions)
            wpt_generator_skill.instructions = template.render(
                has_feature_id=has_feature_id
            )

            skill_toolset = SkillToolset(skills=[wpt_generator_skill])
            tools.append(skill_toolset)
        except Exception as e:
            ui.error(f"Failed to load wpt-generator skill: {e}")
    else:
        ui.warning(
            "wpt-generator skill directory not found. Agent will generate "
            "tests without skill guidance."
        )

    system_template = jinja_env.get_template("adk_test_generator_system.jinja")
    instruction = system_template.render(
        test_type=test_type_enum.value,
    )

    # Prevent ADK's internal template parser from crashing when it encounters
    # WPT syntax like `{{host}}` or `{{variable}}` by mapping them to itself.
    adk_state: dict[str, Any] = {}
    for match in re.finditer(r"\{+([^{}]+)\}+", instruction):
        var_name = match.group(1).strip()
        if var_name.isidentifier():
            adk_state[var_name] = match.group(0)

    # Ensure the agent name is a valid Python identifier.
    safe_root_name = root_name.replace("-", "_").replace(".", "_")
    agent_kwargs: dict[str, Any] = {
        "name": f"wpt_generator_{safe_root_name}",
        "model": create_adk_model(config, model_string),
        "instruction": instruction,
        "tools": list(tools),
    }

    # Enable native thought blocks for compatible Gemini models
    if config.provider.lower() == "gemini":
        model_lower = model_string.lower()
        if "pro" in model_lower or "thinking" in model_lower:
            agent_kwargs["generate_content_config"] = (
                types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(include_thoughts=True)
                )
            )

    agent = Agent(**agent_kwargs)

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="wpt-gen",
        user_id="cli_user",
        session_id=f"gen_{root_name}",
        state=adk_state,
    )
    runner = Runner(
        agent=agent, app_name="wpt-gen", session_service=session_service
    )

    # Configure output directory context
    if config.output_dir:
        output_dir = Path(config.output_dir).resolve()
    else:
        output_dir = wpt_root

    # Context every agent would otherwise fetch, list or load itself, put
    # before the suggestion so agents of one run share a prompt prefix.
    spec_contents = context.spec_contents or {}
    if (
        sum(len(text) for text in spec_contents.values())
        > MAX_PROMPT_SPEC_CHARS
    ):
        spec_contents = {}
    target_directory = extract_xml_tag(suggestion_xml, "target_directory")
    style_guide = skill_dir / "references" / "wpt_style_guide.md"
    prompt_template = jinja_env.get_template("adk_test_generator.jinja")
    prompt = prompt_template.render(
        output_dir=output_dir,
        suggestion_xml=suggestion_xml,
        spec_contents=spec_contents,
        target_directory=target_directory,
        directory_listing=_directory_listing(wpt_root, target_directory),
        wpt_style_guide=(
            style_guide.read_text(encoding="utf-8")
            if style_guide.is_file()
            else None
        ),
    )
    content = types.Content(role="user", parts=[types.Part(text=prompt)])

    try:
        events = runner.run_async(
            session_id=session.id, user_id="cli_user", new_message=content
        )

        # We just consume the stream to let the agent run.
        start = time.monotonic()
        model_calls = tool_calls = 0
        with ADKStreamManager(
            ui, config=StreamConfig(include_thoughts=config.include_thoughts)
        ) as stream_manager:
            async for event in events:
                stream_manager.process_event(event)
                if getattr(event, "usage_metadata", None) is not None:
                    model_calls += 1
                parts = event.content.parts if event.content else None
                tool_calls += sum(
                    1 for part in parts or [] if part.function_call
                )
        agent_seconds = time.monotonic() - start

        results = []
        if not generated_paths:
            ui.warning("Agent finished but did not report any generated paths.")

        # If the agent correctly called the completion tool, we read the files
        for path_str in generated_paths:
            try:
                target_path = Path(path_str)
                # Ensure it is absolutely relative to wpt_root
                target_path = _validate_safe_path(target_path, wpt_root)

                if target_path.is_file():
                    file_content = target_path.read_text(encoding="utf-8")
                    results.append((target_path, file_content, suggestion_xml))
            except (ValueError, OSError) as e:
                ui.error(
                    f"Failed to read securely generated file '{path_str}': {e}"
                )

        start = time.monotonic()
        _record_final_runs(
            [path for path, _, _ in results],
            run_log,
            reported_failures,
            context,
            config,
            wpt_root,
            ui,
        )
        usage = stream_manager.token_usage
        context.agent_usage.append(
            {
                "suggestion": extract_xml_tag(suggestion_xml, "description"),
                "files": [path.name for path, _, _ in results],
                "seconds": round(agent_seconds, 1),
                "model_calls": model_calls,
                "tool_calls": tool_calls,
                "prompt_tokens": usage.prompt_tokens,
                "cached_tokens": usage.cached_tokens,
                "output_tokens": usage.candidates_tokens,
                "total_tokens": usage.total_tokens,
                "test_runs": len(run_log.runs),
                "test_run_seconds": round(
                    sum(run.get("duration") or 0 for run in run_log.runs), 1
                ),
                "final_run_seconds": round(time.monotonic() - start, 1),
            }
        )
        return results

    finally:
        await runner.close()
        await session_service.delete_session(
            app_name="wpt-gen", user_id="cli_user", session_id=session.id
        )


def _record_final_runs(
    paths: list[Path],
    run_log: RunLog,
    reported_failures: list[dict[str, Any]],
    context: WorkflowContext,
    config: Config,
    wpt_root: Path,
    ui: UIProvider,
) -> None:
    """Records each generated test's final local run in `context.run_records`.

    The agent's last run of a test is reused if no file changed after it;
    otherwise the harness runs the test once more. Manual tests, and every
    test under `--skip-run`, are recorded as not run.
    """
    root = wpt_root.resolve()
    rel_paths = [path.resolve().relative_to(root).as_posix() for path in paths]
    try:
        test_types = classify_test_files(root, rel_paths)
    except WebFeaturesYmlError as e:
        ui.warning(f"Generated tests weren't given a final run: {e}")
        return
    for path, rel_path in zip(paths, rel_paths, strict=True):
        test_type = test_types.get(rel_path, "support")
        if test_type == "support":
            continue
        agent_runs = [run for run in run_log.runs if run["path"] == rel_path]
        final: dict[str, Any]
        if test_type == "manual" or config.skip_run:
            reason = "manual test" if test_type == "manual" else "--skip-run"
            final = {"path": rel_path, "status": "not_run", "reason": reason}
        elif agent_runs and agent_runs[-1]["changes"] == run_log.changes:
            final = {**agent_runs[-1], "started_by": "agent"}
        else:
            ui.info(f"Running {rel_path} once more for its run record...")
            _, final = run_test_file(
                root, rel_path, config.run_on_browser, config.run_on_channel, ui
            )
            final["started_by"] = "harness"
        failures = []
        for failure in reported_failures:
            try:
                failure_path = _validate_safe_path(
                    Path(str(failure.get("path"))), wpt_root
                )
            except ValueError:
                continue
            if failure_path.resolve() == path.resolve():
                failures.append(
                    {
                        "subtest": str(failure.get("subtest") or ""),
                        "reason": str(failure.get("reason") or ""),
                    }
                )
        context.run_records[str(path)] = {
            "type": test_type,
            "final": final,
            "agent_runs": [
                {
                    "attempt": run["attempt"],
                    "status": run["status"],
                    "failing": _failing_subtests(run),
                    "duration": run.get("duration"),
                }
                for run in agent_runs
            ],
            "known_failures": failures,
            "lint": _final_lints(root, rel_path, run_log),
        }


def _final_lints(
    root: Path, rel_path: str, run_log: RunLog
) -> dict[str, dict[str, Any]]:
    """Each linter's final result for a generated test.

    As with runs, the agent's last lint is reused if no file changed after
    it; otherwise the harness lints the file again.
    """
    lints = {}
    for tool in ("wpt_lint", "lint_ext"):
        agent_lints = [
            lint
            for lint in run_log.lints
            if lint["path"] == rel_path and lint["tool"] == tool
        ]
        if agent_lints and agent_lints[-1]["changes"] == run_log.changes:
            result, started_by = agent_lints[-1]["result"], "agent"
        else:
            started_by = "harness"
            try:
                result = (
                    lint_file(root, rel_path)
                    if tool == "wpt_lint"
                    else lint_ext_file(root / rel_path)
                )
            except (OSError, subprocess.SubprocessError) as e:
                result = {"status": "error", "error": str(e)}
        if result["status"] == "failed":
            findings = result.get("lint_output", "").splitlines()
            for f in result.get("findings", []):
                path, line, rule = f["path"], f["line"], f["rule_id"]
                description = f["description"]
                findings.append(f"{path}:{line}: {description} ({rule})")
        else:
            findings = [result["error"]] if result["status"] == "error" else []
        lints[tool] = {
            "status": {"success": "clean", "failed": "findings"}.get(
                result["status"], "error"
            ),
            "findings": findings,
            "started_by": started_by,
        }
    return lints


def _directory_listing(wpt_root: Path, directory: str | None) -> str | None:
    """Lists a directory's entries, one per line, directories with a `/`."""
    if not directory:
        return None
    try:
        target = _validate_safe_path(Path(directory), wpt_root)
        entries = sorted(target.iterdir())
    except (OSError, ValueError):
        return None
    names = [e.name + ("/" if e.is_dir() else "") for e in entries]
    if len(names) > MAX_LISTING_ENTRIES:
        rest = len(names) - MAX_LISTING_ENTRIES
        names = names[:MAX_LISTING_ENTRIES] + [f"... and {rest} more"]
    return "\n".join(names)


def _failing_subtests(run: dict[str, Any]) -> list[str]:
    """Names each failing test or subtest in a run record."""
    failing = []
    for result in run.get("results", []):
        test, status = result["test"], result["status"]
        if status not in ("OK", "PASS"):
            failing.append(f"{test}: {status}")
        for subtest in result["subtests"]:
            name, status = subtest["name"], subtest["status"]
            if status != "PASS":
                failing.append(f"{name}: {status}")
    return failing
