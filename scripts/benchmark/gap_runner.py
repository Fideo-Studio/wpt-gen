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

"""CI-only gap-analysis suite runner.

Replays frozen fixtures through the coverage audit, scores the current gap
analysis, and writes ``report.json``, ``report.md``, and ``score.json``. No
generated tests are produced or scored on this path.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from benchmark.gap_fixtures import REPO_ROOT, inject_fixture  # noqa: E402
from benchmark.gap_manifest import GapEntry, load_gap_manifest  # noqa: E402
from benchmark.gap_payload import GapPayload  # noqa: E402
from benchmark.gap_score_report import (  # noqa: E402
    build_report_markdown,
    write_score,
)
from benchmark.gap_scoring import (  # noqa: E402
    MECHANICAL_CHECKS,
    UNMAPPED_SUGGESTIONS_KEY,
    GapHarnessError,
    GapScoreSummary,
    check_run_mechanics,
    resolve_run_model_provider,
    summarize_flip_rates,
)
from wptgen.phases.report_render import (  # noqa: E402
    RequirementAudit,
    parse_audit_worksheet,
    parse_test_suggestions,
)
from wptgen.utils import extract_xml_tag, parse_suggestions  # noqa: E402

RESUME_BANNER = "Explicitly resuming workflow from: coverage_audit"
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 5.0
AUDIT_TIMEOUT_SECONDS = 600


def assert_no_generation(run_dir: Path) -> None:
    """Reject generated-test artifacts in a CI-only gap run dir."""
    generated = run_dir / "generated_tests.json"
    nested = run_dir / "generated_tests" / "generated_tests.json"
    html_files = sorted(run_dir.glob("*.html")) + sorted(
        (run_dir / "generated_tests").glob("*.html")
    )
    if generated.exists() or nested.exists() or html_files:
        raise GapHarnessError(
            f"generate-guard: generated-test artifacts in {run_dir}"
        )


def gap_cli_args(
    feature: str,
    rep_dir: Path,
    wpt_dir: Path,
    config: Path | None = None,
    provider: str | None = None,
) -> list[str]:
    """Build the exact resume-at-coverage-audit command for one repeat."""
    cmd = [
        "wpt-gen",
        "generate",
        feature,
        "--wpt-dir",
        str(wpt_dir),
        "--suggestions-only",
        "--resume-from",
        "coverage_audit",
        "--state-dir",
        str(rep_dir),
        "--no-cache",
        "--yes-tokens",
    ]
    if config is not None:
        cmd.extend(["--config", str(config)])
    if provider:
        cmd.extend(["--provider", provider])
    return cmd


def _git_status(wpt_dir: Path) -> str:
    """Return the pinned checkout's porcelain status."""
    result = subprocess.run(
        ["git", "-C", str(wpt_dir), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ},
    )
    return result.stdout


def _record_error(rep_dir: Path, message: str) -> None:
    """Record a loud failure so the mechanical checks cannot miss it."""
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / "error.json").write_text(
        json.dumps({"failed": True, "error": message}, indent=2) + "\n",
        encoding="utf-8",
    )


def _is_retryable_failure(completed: subprocess.CompletedProcess[str]) -> bool:
    """Identify transient rate limits without retrying exhausted billing."""
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    lowered = combined.lower()
    if "402" in combined or "prepayment credits are depleted" in lowered:
        return False
    return any(
        term in combined
        for term in ("429", "RESOURCE_EXHAUSTED", "ResourceExhausted", "503")
    )


def run_gap_audit(
    entry: GapEntry,
    rep_dir: Path,
    fixture_dir: Path,
    wpt_dir: Path,
    *,
    pin: str,
    canary: str,
    config: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """Inject one fixture and replay only its coverage audit."""
    try:
        git_before = _git_status(wpt_dir)
        inject_fixture(
            entry.feature,
            fixture_dir,
            wpt_dir,
            rep_dir,
            pin=pin,
            canary=canary,
        )
        assert_no_generation(rep_dir)

        cmd = gap_cli_args(
            entry.feature, rep_dir, wpt_dir, config=config, provider=provider
        )
        completed: subprocess.CompletedProcess[str] | None = None
        for attempt in range(MAX_RETRIES):
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=AUDIT_TIMEOUT_SECONDS,
                env={**os.environ},
            )
            if completed.returncode == 0:
                break
            if _is_retryable_failure(completed) and attempt < MAX_RETRIES - 1:
                time.sleep(
                    random.uniform(0, BASE_BACKOFF_SECONDS * (2**attempt))
                )
                continue
            break

        assert completed is not None
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            detail = (stderr or stdout).strip()
            _record_error(
                rep_dir,
                f"wpt-gen exited {completed.returncode}: {detail}",
            )
            return
        if RESUME_BANNER not in stdout:
            _record_error(
                rep_dir, f"missing exact resume banner: {RESUME_BANNER}"
            )
            return

        assert_no_generation(rep_dir)
        if provider or model:
            (rep_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "provider": provider or "",
                        "model": model or "",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        if _git_status(wpt_dir) != git_before:
            _record_error(
                rep_dir, "WPT checkout changed during fixture injection"
            )
    except (GapHarnessError, subprocess.SubprocessError, OSError) as exc:
        _record_error(rep_dir, str(exc))


def _requirement_rows(path: Path) -> list[dict[str, str]]:
    """Read frozen requirements, supporting both the XML and list shapes."""
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "requirements_xml" in data:
        xml = str(data.get("requirements_xml") or "")
        try:
            root = ElementTree.fromstring(f"<root>{xml}</root>")
        except ElementTree.ParseError as exc:
            raise GapHarnessError(f"invalid requirements XML: {path}") from exc
        return [
            {
                "id": str(node.get("id") or ""),
                "spec_quote": str(node.findtext("spec_quote") or ""),
            }
            for node in root.iter("requirement")
        ]
    if isinstance(data, list):
        return [
            {
                "id": str(row.get("id") or ""),
                "spec_quote": str(row.get("spec_quote") or ""),
            }
            for row in data
        ]
    raise GapHarnessError(f"unsupported requirements.json shape: {path}")


def _audit_text(run_dir: Path) -> str:
    """Read the audit response from a run dir, JSON-wrapped or raw XML."""
    path = run_dir / "test_suggestions.json"
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8")
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, dict):
        return str(data.get("audit_response") or "")
    return raw


def _normalize_text(text: str) -> str:
    """Collapse whitespace for permissive suggestion-to-requirement matching."""
    return " ".join(text.split())


def map_suggestions_to_requirements(
    audit: str,
    rows: list[RequirementAudit],
    quotes_by_id: dict[str, str],
) -> dict[str, list[str]]:
    """Map audit suggestions onto the frozen UNCOVERED requirement IDs."""
    suggestions_xml = extract_xml_tag(audit, "test_suggestions") or audit
    suggestions = parse_test_suggestions(suggestions_xml)
    raw_blocks = parse_suggestions(suggestions_xml)
    candidates: list[dict[str, Any]] = []
    for index, suggestion in enumerate(suggestions):
        raw = raw_blocks[index] if index < len(raw_blocks) else ""
        candidates.append(
            {
                "requirement_id": extract_xml_tag(raw, "requirement_id") or "",
                "description": suggestion.description,
                "spec_quote": extract_xml_tag(raw, "spec_quote") or "",
                "used": False,
            }
        )

    mapped: dict[str, list[str]] = {}

    def assign(req_id: str, candidate: dict[str, Any]) -> None:
        mapped.setdefault(req_id, []).append(candidate["description"])
        candidate["used"] = True

    def first_unused(
        predicate: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any] | None:
        return next(
            (c for c in candidates if not c["used"] and predicate(c)), None
        )

    def by_requirement_id(
        req_id: str,
    ) -> Callable[[dict[str, Any]], bool]:
        def predicate(candidate: dict[str, Any]) -> bool:
            return str(candidate["requirement_id"]) == req_id

        return predicate

    def by_spec_quote(expected: str) -> Callable[[dict[str, Any]], bool]:
        def predicate(candidate: dict[str, Any]) -> bool:
            return _normalize_text(candidate["spec_quote"]) == expected

        return predicate

    def by_description(expected: str) -> Callable[[dict[str, Any]], bool]:
        def predicate(candidate: dict[str, Any]) -> bool:
            return _normalize_text(candidate["description"]) == expected

        return predicate

    uncovered = [row for row in rows if row.status == "UNCOVERED"]
    for row in uncovered:
        candidate = first_unused(by_requirement_id(row.id))
        if candidate is not None:
            assign(row.id, candidate)
    for row in uncovered:
        if mapped.get(row.id):
            continue
        quote = _normalize_text(quotes_by_id.get(row.id, ""))
        if not quote:
            continue
        candidate = first_unused(by_spec_quote(quote))
        if candidate is not None:
            assign(row.id, candidate)
    for row in uncovered:
        if mapped.get(row.id):
            continue
        description = _normalize_text(row.text)
        if not description:
            continue
        candidate = first_unused(by_description(description))
        if candidate is not None:
            assign(row.id, candidate)
    for row, candidate in zip(
        (r for r in uncovered if not mapped.get(r.id)),
        (c for c in candidates if not c["used"]),
        strict=False,
    ):
        assign(row.id, candidate)
    for candidate in candidates:
        if not candidate["used"]:
            mapped.setdefault(UNMAPPED_SUGGESTIONS_KEY, []).append(
                candidate["description"]
            )
    return mapped


def load_repeat_inputs(
    run_dir: Path, feature: str
) -> tuple[GapPayload, set[str], list[str], str, list[str]]:
    """Build the scorer inputs from one injected repeat directory."""
    req_path = run_dir / "requirements.json"
    if not req_path.exists():
        raise GapHarnessError(f"missing requirements.json: {run_dir}")
    requirements = _requirement_rows(req_path)
    input_ids = {row["id"] for row in requirements if row["id"]}
    quotes = [row["spec_quote"] for row in requirements if row["spec_quote"]]
    quotes_by_id = {
        row["id"]: row["spec_quote"]
        for row in requirements
        if row["id"] and row["spec_quote"]
    }

    resume_path = run_dir / f"resume_{feature}.json"
    resume: Any = json.loads(resume_path.read_text(encoding="utf-8"))
    spec_contents = resume.get("spec_contents") or {}
    snapshot = "\n".join(str(value) for value in spec_contents.values())
    context = resume.get("wpt_context") or {}
    batch_paths = set(context.get("test_contents") or {})

    audit = _audit_text(run_dir)
    worksheet = extract_xml_tag(audit, "audit_worksheet") or ""
    rows = parse_audit_worksheet(worksheet)
    suggestions_by_requirement = map_suggestions_to_requirements(
        audit, rows, quotes_by_id
    )

    partitions = sorted(run_dir.glob("audit_partition_*.md"))
    partition_texts = (
        [path.read_text(encoding="utf-8") for path in partitions]
        if partitions
        else [worksheet]
    )

    payload: GapPayload = {
        "rows": rows,
        "suggestions_by_requirement": suggestions_by_requirement,
        "batch_paths": batch_paths,
        "spec_quotes": quotes,
        "spec_quotes_by_requirement": quotes_by_id,
    }
    return payload, input_ids, quotes, snapshot, partition_texts


def discover_gap_runs(
    out: Path, entries: list[GapEntry], repeats: int
) -> tuple[list[GapEntry], int]:
    """Narrow ``--score-only`` entries to the run dirs present on disk."""
    present: list[GapEntry] = []
    found = 0
    for entry in entries:
        entry_dir = out / "runs" / entry.id
        if not entry_dir.is_dir():
            continue
        indices = [
            int(path.name[len("rep-") :])
            for path in entry_dir.glob("rep-*")
            if path.is_dir() and path.name[len("rep-") :].isdigit()
        ]
        if not indices:
            continue
        present.append(entry)
        found = max(found, max(indices) + 1)
    return present, found


def _fixture_dir(path: str) -> Path:
    """Resolve a manifest fixture path against the repository root."""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def _build_parser() -> argparse.ArgumentParser:
    """Build the gap-suite CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wpt-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional wpt-gen.yml passed through to each audit run.",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help="Provider recorded in run metadata and passed to wpt-gen.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model recorded in run metadata; never passed to wpt-gen.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the gap suite and write its score and reports."""
    args = _build_parser().parse_args(argv)
    if args.repeats < 1:
        raise GapHarnessError("--repeats must be at least 1")
    if args.jobs < 1:
        raise GapHarnessError("--jobs must be at least 1")

    pin, canary, entries, fixtures = load_gap_manifest(args.manifest)
    selected = entries
    repeats = args.repeats
    if args.score_only:
        selected, repeats = discover_gap_runs(args.out, selected, repeats)
        if not selected or repeats == 0:
            raise GapHarnessError("--score-only found no run directories")

    tasks = [
        (entry, rep, args.out / "runs" / entry.id / f"rep-{rep}")
        for entry in selected
        for rep in range(repeats)
    ]
    if not args.score_only:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [
                pool.submit(
                    run_gap_audit,
                    entry,
                    rep_dir,
                    _fixture_dir(fixtures[entry.feature].path),
                    args.wpt_dir,
                    pin=pin,
                    canary=canary,
                    config=args.config,
                    provider=args.provider,
                    model=args.model,
                )
                for entry, _, rep_dir in tasks
            ]
            for future in as_completed(futures):
                future.result()

    repeats_by_entry: dict[str, dict[str, GapPayload]] = {}
    frozen_ids_by_entry: dict[str, set[str]] = {}
    kind_by_entry = {entry.id: entry.kind for entry in selected}
    mechanical_failures = 0
    for entry, rep, rep_dir in tasks:
        try:
            (
                payload,
                input_ids,
                quotes,
                snapshot,
                partition_texts,
            ) = load_repeat_inputs(rep_dir, entry.feature)
        except (GapHarnessError, OSError, json.JSONDecodeError) as exc:
            mechanical_failures += 1
            sys.stderr.write(f"[warn] {entry.id} rep-{rep}: {exc}\n")
            continue
        frozen_ids_by_entry[entry.id] = input_ids
        repeats_by_entry.setdefault(entry.id, {})[f"rep-{rep}"] = payload
        mechanical_failures += len(
            check_run_mechanics(
                rep_dir,
                payload,
                input_ids,
                quotes,
                snapshot,
                pin,
                canary,
                partition_texts,
            )
        )

    model, provider = resolve_run_model_provider(
        [rep_dir for _, _, rep_dir in tasks]
    )
    flip_report = summarize_flip_rates(
        repeats_by_entry, frozen_ids_by_entry, kind_by_entry
    )
    summary = GapScoreSummary(
        verdict_flip_rate=flip_report["overall"],
        mechanical_failures=mechanical_failures,
        model=model,
        provider=provider,
    )
    report_json: dict[str, Any] = {
        "summary": asdict(summary),
        "flip_rates": flip_report,
        "wpt_pin": pin,
        "manifest": str(args.manifest),
        "repeats": repeats,
        "mechanical_checks": list(MECHANICAL_CHECKS),
        "entries": [
            {
                "id": entry.id,
                "kind": entry.kind,
                "flip_rate": flip_report["by_entry"].get(entry.id),
            }
            for entry in selected
        ],
    }

    write_score(args.out, summary)
    (args.out / "report.json").write_text(
        json.dumps(report_json, indent=2) + "\n", encoding="utf-8"
    )
    report_md = build_report_markdown(report_json)
    (args.out / "report.md").write_text(report_md, encoding="utf-8")

    return 1 if mechanical_failures > 0 else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GapHarnessError as exc:
        sys.stderr.write(f"error: {exc}\n")
        raise SystemExit(1) from exc
