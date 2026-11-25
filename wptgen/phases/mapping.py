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

"""Phase 5: Mapping - Recording generated tests in WEB_FEATURES.yml."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wptgen.config import Config
from wptgen.context import fetch_feature_yaml
from wptgen.metadata import (
    ACTION_ALREADY_MAPPED,
    MappingChange,
    WebFeaturesYmlError,
    classify_test_files,
    current_features,
    map_test,
)
from wptgen.models import WorkflowContext, WorkflowError
from wptgen.ui import UIProvider
from wptgen.web_features_manifest import (
    WebFeaturesManifestError,
    build_manifest_at,
    features_for_paths,
    load_manifest_data,
    url_to_source_path,
)

AFTER_MANIFEST_FILENAME = "web_features_manifest.after.json"
REPORT_FILENAME = "web_features_mapping.json"


@dataclass
class PlannedMapping:
    """The features one generated test will be mapped to."""

    test_path: str
    feature_ids: list[str]
    reason: str = ""
    review: list[str] = field(default_factory=list)


@dataclass
class MappingReport:
    """What the mapping phase did, saved as `web_features_mapping.json`."""

    feature_id: str
    planned: list[PlannedMapping] = field(default_factory=list)
    changes: list[MappingChange] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    approved: bool = True
    after_manifest: str | None = None
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_id": self.feature_id,
            "planned": [asdict(p) for p in self.planned],
            "changes": [c.to_dict() for c in self.changes],
            "skipped": self.skipped,
            "approved": self.approved,
            "after_manifest": self.after_manifest,
            "problems": self.problems,
        }


def _ids(feature_ids: list[str]) -> str:
    return ", ".join(feature_ids)


def _plan(
    context: WorkflowContext,
    wpt_root: Path,
    tests: list[str],
    kept: dict[str, list[str]],
) -> list[PlannedMapping]:
    """Combines each test's declared features with its existing mapping."""
    feature_id = context.feature_id or ""
    planned = []
    for rel in tests:
        declared = context.feature_mappings.get(str(wpt_root / rel), {})
        ids = list(declared.get("feature_ids") or [feature_id])
        reason = declared.get("reason", "")
        review = []
        if feature_id not in ids:
            ids.insert(0, feature_id)
            review.append(f"{feature_id} wasn't declared; added")
        others = [f for f in ids if f != feature_id]
        if others:
            why = reason or "no reason given"
            review.append(f"also covers {_ids(others)}: {why}")
        previous = [f for f in kept.get(rel, []) if f not in ids]
        if previous:
            review.append(f"already mapped to {_ids(previous)}; kept")
        ids += previous
        current = current_features(wpt_root, Path(rel))
        extra = [f for f in current if f not in ids]
        if extra and set(ids) <= set(current):
            review.append(f"the directory's rules also map it to {_ids(extra)}")
        planned.append(PlannedMapping(rel, ids, reason, review))
    return planned


def _unknown_features(
    planned: list[PlannedMapping], known: set[str]
) -> list[str]:
    ids = {f for p in planned for f in p.feature_ids} - known
    return sorted(f for f in ids if fetch_feature_yaml(f) is None)


def _lost_mappings(
    before_path: str, after_path: Path, wpt_root: Path, edited: set[str]
) -> list[str]:
    """Lists tests under an edited directory that lost a mapping."""
    before = load_manifest_data(before_path)
    after = load_manifest_data(after_path)
    lost = []
    for feature, urls in before.items():
        for url in sorted(set(urls) - set(after.get(feature, []))):
            rel = url.split("?", 1)[0].lstrip("/")
            if any(d == "" or rel.startswith(f"{d}/") for d in edited) and (
                url_to_source_path(url, wpt_root) is not None
            ):
                lost.append(f"{url} is no longer mapped to {feature}")
    return lost


def _write_report(state_dir: Path, report: MappingReport) -> None:
    with open(state_dir / REPORT_FILENAME, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2)


def run_web_features_mapping(
    context: WorkflowContext,
    config: Config,
    ui: UIProvider,
    state_dir: Path,
) -> MappingReport:
    """Maps generated tests to the features the generator declared for them.

    The planned mappings are listed, with notes on any that need a judgment
    call (more than the target feature, or an existing mapping that's kept),
    and confirmed before anything is written unless `--yes-tests` is set.
    The result is checked by rebuilding WPT's web-features manifest.

    Raises:
        WorkflowError: If a mapping can't be written or doesn't show up in
            the rebuilt manifest. Edits already made are left for review.
    """
    if not context.feature_id or not config.wpt_path:
        raise WorkflowError(
            "Mapping needs a web feature ID and a WPT checkout."
        )
    feature_id = context.feature_id
    wpt_root = Path(config.wpt_path).resolve()
    report = MappingReport(feature_id=feature_id)

    ui.on_phase_start(5, "Web Features Mapping")

    rel_paths: list[str] = []
    for path, _content, _suggestion in context.generated_tests or []:
        try:
            rel = Path(path).resolve().relative_to(wpt_root).as_posix()
        except ValueError:
            report.skipped[str(path)] = "outside the WPT checkout"
            continue
        if rel not in rel_paths:
            rel_paths.append(rel)

    try:
        types = classify_test_files(wpt_root, rel_paths)
        tests = [r for r in rel_paths if types.get(r, "support") != "support"]
        report.skipped.update(
            {r: "not a test file" for r in rel_paths if r not in tests}
        )
        if not tests:
            ui.warning("No generated test files to map.")
            _write_report(state_dir, report)
            return report

        before = (
            context.web_features_manifest.path
            if context.web_features_manifest
            else None
        )
        kept = features_for_paths(before, tests) if before else {}
        report.planned = _plan(context, wpt_root, tests, kept)

        known = {feature_id} | (
            set(load_manifest_data(before)) if before else set()
        )
        unknown = _unknown_features(report.planned, known)
        if unknown:
            report.problems.append(f"Unknown web feature IDs: {_ids(unknown)}")
            raise WorkflowError(report.problems[-1])

        for p in report.planned:
            ui.info(f"{p.test_path}: {_ids(p.feature_ids)}")
            for note in p.review:
                ui.warning(f"  Review: {note}")
        if not config.yes_tests:
            if not ui.confirm("Write these WEB_FEATURES.yml mappings?"):
                report.approved = False
                ui.warning("Mapping skipped; the generated tests are unmapped.")
                _write_report(state_dir, report)
                return report

        report.changes = [
            map_test(wpt_root, Path(p.test_path), p.feature_ids)
            for p in report.planned
        ]
        if all(c.action == ACTION_ALREADY_MAPPED for c in report.changes):
            _write_report(state_dir, report)
            ui.success("Every generated test was already mapped.")
            ui.on_phase_complete("Web Features Mapping")
            return report

        ui.info("Rebuilding the web-features manifest to check the mapping...")
        after = build_manifest_at(wpt_root, state_dir / AFTER_MANIFEST_FILENAME)
        report.after_manifest = str(after)
        mapped = features_for_paths(after, tests)
        for p in report.planned:
            missing = [f for f in p.feature_ids if f not in mapped[p.test_path]]
            if missing:
                report.problems.append(
                    f"{p.test_path} is not mapped to {_ids(missing)} in "
                    "the rebuilt manifest"
                )
        if before:
            edited = {
                "" if parent == "." else parent
                for c in report.changes
                if c.action != ACTION_ALREADY_MAPPED
                for parent in [Path(c.yml_path).parent.as_posix()]
            }
            report.problems += _lost_mappings(before, after, wpt_root, edited)
    except (WebFeaturesYmlError, WebFeaturesManifestError) as e:
        report.problems.append(str(e))
        _write_report(state_dir, report)
        raise WorkflowError(f"Mapping tests to {feature_id} failed: {e}") from e
    except WorkflowError:
        _write_report(state_dir, report)
        raise

    _write_report(state_dir, report)
    if report.problems:
        for problem in report.problems:
            ui.error(problem)
        raise WorkflowError(
            "The rebuilt web-features manifest doesn't match the mapping. "
            f"See {state_dir / REPORT_FILENAME}."
        )

    ui.success(f"Mapped {len(tests)} test(s); the rebuilt manifest agrees.")
    ui.on_phase_complete("Web Features Mapping")
    return report
