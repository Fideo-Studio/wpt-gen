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
"""Runs `wpt-gen evaluate` on every test a `wpt-gen generate` run wrote.

Reads `generated_tests.json` from the run's state directory and evaluates each
test against the spec URLs of the suggestion it was generated from. Findings
reports are written to `<state dir>/evaluations/`, under each test's
directory. References and helper files are skipped.

Usage:
    python scripts/evaluate_generated_tests.py STATE_DIR [-- EVALUATE_ARGS...]

Arguments after `--` are passed to every `wpt-gen evaluate` call, for example
`-- --provider gemini --wpt-dir ~/wpt`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from wpt_utils import is_wpt_test_file


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("evaluate_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    extra = [a for a in args.evaluate_args if a != "--"]

    wpt_gen = shutil.which("wpt-gen")
    if not wpt_gen:
        print("wpt-gen isn't on PATH; activate its environment first.")
        return 1
    tests_json = args.state_dir / "generated_tests.json"
    if not tests_json.is_file():
        print(f"No generated_tests.json in {args.state_dir}.")
        return 1

    failed = []
    entries = json.loads(tests_json.read_text(encoding="utf-8"))
    for entry in entries:
        path = Path(entry["path"])
        run = entry.get("run")
        # Run records exist for tests only; older runs don't have them.
        if run is None and (not is_wpt_test_file(path) or "-ref" in path.name):
            continue
        spec_urls = re.findall(
            r"<spec_url>\s*(.*?)\s*</spec_url>", entry.get("suggestion") or ""
        )
        output_dir = args.state_dir / "evaluations" / path.parent.name
        cmd = [wpt_gen, "evaluate", str(path), "--output-dir", str(output_dir)]
        if spec_urls:
            cmd += ["--spec-urls", ",".join(spec_urls)]
        print(f"Evaluating {path}...", flush=True)
        if subprocess.run(cmd + extra, check=False).returncode != 0:
            failed.append(str(path))

    if failed:
        print(f"{len(failed)} evaluation(s) failed:")
        for path_str in failed:
            print(f"  {path_str}")
        return 1
    print(f"Findings reports are in {args.state_dir / 'evaluations'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
