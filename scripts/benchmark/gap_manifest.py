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
"""Manifest loading and validation for the gap scoring.

Extend scripts/benchmark/ rather than writing a second pipeline: reuse the
retry/429 backoff in run_single, --jobs, --repeats, --score-only, the report
writer and check_quality_gates from run_benchmark.py. Add a gap entry type and
a parser for benchmarks/gap/manifest.yaml alongside scripts/benchmark/manifest.py,
and a gap scoring module next to scoring.py.

"""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class GapEntry:
    id: str
    feature: str
    fixture: str
    kind: str
    stage: str


@dataclass(frozen=True)
class FixtureEntry:
    feature: str
    reason: str
    path: str


def load_gap_manifest(
    path: Path,
) -> tuple[str, str, list[GapEntry], dict[str, FixtureEntry]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = data.get("gap_corpus", [])
    entries = [
        GapEntry(
            id=row["id"],
            feature=row["feature"],
            fixture=row["fixture"],
            kind=row["kind"],
            stage=row["stage"],
        )
        for row in rows
    ]
    fixtures = {
        feature: FixtureEntry(
            feature=feature, reason=row["reason"], path=row["path"]
        )
        for feature, row in data.get("fixtures", {}).items()
    }
    return (
        data["wpt_upstream_commit"],
        data["canary"],
        entries,
        fixtures,
    )
