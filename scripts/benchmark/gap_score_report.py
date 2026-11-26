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

"""Score output and report formatting for the gap benchmark."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from benchmark.gap_scoring import GapScoreSummary


def _format_float(value: float | None) -> str:
    """Format a metric, using n/a when the metric was not recorded."""
    return "n/a" if value is None else f"{value:.2f}"


def _format_int(value: int | None) -> str:
    """Format a count, using n/a when the count was not recorded."""
    return "n/a" if value is None else str(value)


def write_score(out_dir: Path, summary: GapScoreSummary) -> None:
    """Write the machine-readable score file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(summary), indent=2)
    (out_dir / "score.json").write_text(payload, encoding="utf-8")


def build_report_markdown(report_json: dict[str, Any]) -> str:
    """Render the human-readable gap report, including flip-rate breakdowns."""
    summary: dict[str, Any] = report_json.get("summary", {})
    flips: dict[str, Any] = report_json.get("flip_rates", {})
    lines = [
        "# Gap Analysis Report",
        "",
        (
            f"WPT pin: `{report_json.get('wpt_pin', 'n/a')}` · "
            f"repeats: {report_json.get('repeats', 'n/a')} · "
            f"model: {summary.get('model') or 'n/a'} · "
            f"provider: {summary.get('provider') or 'n/a'}"
        ),
        "",
        "## Mechanical checks",
        "",
        (
            f"Failures: {_format_int(summary.get('mechanical_failures'))}. "
            "These gate the run."
        ),
        "",
    ]
    for name in report_json.get("mechanical_checks", []):
        lines.append(f"- `{name}`")
    lines.extend(
        ["", "## Verdict flip rate", "", "Report only; never gated.", ""]
    )
    overall = flips.get("overall")
    lines.append(f"Overall: {_format_float(overall)}")
    lines.extend(
        [
            "",
            "### By feature type",
            "",
            "| kind | flip rate |",
            "| --- | ---: |",
        ]
    )
    for kind, rate in sorted(flips.get("by_kind", {}).items()):
        lines.append(f"| {kind} | {_format_float(rate)} |")
    lines.extend(
        ["", "### By entry", "", "| entry | flip rate |", "| --- | ---: |"]
    )
    for entry_id, rate in sorted(flips.get("by_entry", {}).items()):
        lines.append(f"| {entry_id} | {_format_float(rate)} |")
    return "\n".join(lines) + "\n"
