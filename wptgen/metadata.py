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

"""Reading and writing WPT's `WEB_FEATURES.yml` files (RFC 237 format)."""

import json
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

WEB_FEATURES_YML = "WEB_FEATURES.yml"
RECURSIVE = "**"
CLASSIFY_TIMEOUT_SECONDS = 120

ACTION_ALREADY_MAPPED = "already mapped"
ACTION_ADDED_RULE = "added rule"
ACTION_UPDATED_RULE = "updated rule"
ACTION_CREATED_FILE = "created file"


class WebFeaturesYmlError(RuntimeError):
    """Raised when a `WEB_FEATURES.yml` file can't be read or safely edited."""


@dataclass(frozen=True)
class Rule:
    """A `- <pattern>: <feature ids>` rule and the lines it spans."""

    pattern: str
    feature_ids: tuple[str, ...]
    start_line: int
    end_line: int  # exclusive

    def matches(self, filename: str) -> bool:
        # As in WPT: `**` takes every file in the directory, other patterns
        # match base names only.
        if self.pattern == RECURSIVE:
            return True
        if "*" in self.pattern:
            return fnmatchcase(filename, self.pattern)
        return filename == self.pattern


@dataclass(frozen=True)
class MappingChange:
    """The result of mapping one test file."""

    test_path: str
    yml_path: str
    action: str
    feature_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "test_path": self.test_path,
            "yml_path": self.yml_path,
            "action": self.action,
            "feature_ids": list(self.feature_ids),
        }


def _feature_ids(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, dict):
        value = value.get("ids")
    if not isinstance(value, list) or not all(
        isinstance(v, str) for v in value
    ):
        raise WebFeaturesYmlError(
            f"{where}: expected a list of feature IDs or {{ids: [...]}}"
        )
    return tuple(value)


def parse_rules(text: str, where: str = WEB_FEATURES_YML) -> list[Rule]:
    """Parses the rules of a `WEB_FEATURES.yml` file, with line positions.

    Raises:
        WebFeaturesYmlError: If the file isn't valid RFC 237 YAML.
    """
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as e:
        raise WebFeaturesYmlError(f"{where}: invalid YAML: {e}") from e

    if not isinstance(root, yaml.MappingNode):
        raise WebFeaturesYmlError(f"{where}: expected a mapping with `rules:`")
    keys = {getattr(k, "value", None): v for k, v in root.value}
    if "rules" not in keys:
        if "features" in keys:
            raise WebFeaturesYmlError(
                f"{where}: uses the old `features:` format; convert it to "
                "`rules:` first."
            )
        raise WebFeaturesYmlError(f"{where}: no `rules:` list")
    rules_node = keys["rules"]
    if not isinstance(rules_node, yaml.SequenceNode) or rules_node.flow_style:
        raise WebFeaturesYmlError(f"{where}: `rules:` must be a block list")

    items = rules_node.value
    rules: list[Rule] = []
    for i, item in enumerate(items):
        if not isinstance(item, yaml.MappingNode) or len(item.value) != 1:
            raise WebFeaturesYmlError(
                f"{where}: rule {i + 1} must have exactly one pattern"
            )
        key_node, value_node = item.value[0]
        pattern = yaml.safe_load(yaml.serialize(key_node))
        if not isinstance(pattern, str):
            raise WebFeaturesYmlError(
                f"{where}: rule {i + 1} pattern must be a string"
            )
        value = yaml.safe_load(yaml.serialize(value_node))

        start = item.start_mark.line
        end = item.end_mark.line + (1 if item.end_mark.column > 0 else 0)
        if i + 1 < len(items):
            end = min(end, items[i + 1].start_mark.line)
        rules.append(
            Rule(
                pattern=pattern,
                feature_ids=_feature_ids(value, f"{where}: rule {i + 1}"),
                start_line=start,
                end_line=max(end, start + 1),
            )
        )
    return rules


def first_matching_rule(rules: list[Rule], filename: str) -> Rule | None:
    """Returns the rule that claims `filename`; the first match wins."""
    return next((r for r in rules if r.matches(filename)), None)


def inherited_features(wpt_root: Path, directory: Path) -> list[str]:
    """Returns the features a directory inherits from its nearest ancestor's
    `**` rules, for a directory with no `WEB_FEATURES.yml` of its own."""
    root = wpt_root.resolve()
    current = directory.resolve().parent
    while True:
        yml = current / WEB_FEATURES_YML
        if yml.is_file():
            ids: list[str] = []
            for rule in parse_rules(_read(yml), _rel(yml, root)):
                if rule.pattern == RECURSIVE:
                    ids.extend(f for f in rule.feature_ids if f not in ids)
            return ids
        if current == root or root not in current.parents:
            return []
        current = current.parent


def current_features(wpt_root: Path, test_rel_path: Path) -> list[str]:
    """Returns the features WPT's rules currently give a test file."""
    directory = wpt_root / test_rel_path.parent
    yml = directory / WEB_FEATURES_YML
    if not yml.is_file():
        return inherited_features(wpt_root, directory)
    rule = first_matching_rule(
        parse_rules(_read(yml), _rel(yml, wpt_root)), test_rel_path.name
    )
    return list(rule.feature_ids) if rule else []


def render_rule(pattern: str, feature_ids: list[str], indent: str = "") -> str:
    """Renders one rule line, e.g. `- foo.html: [grid]`."""
    text = yaml.safe_dump(
        [{pattern: list(feature_ids)}],
        default_flow_style=None,
        sort_keys=False,
        width=10_000,
    )
    if pattern == RECURSIVE:
        text = text.replace("'**':", '"**":', 1)
    return indent + text


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise WebFeaturesYmlError(f"Could not read {path}: {e}") from e


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def map_test(
    wpt_root: Path, test_rel_path: Path, feature_ids: list[str]
) -> MappingChange:
    """Makes WPT's rules map a test file to at least `feature_ids`.

    Edits are made as text, keeping comments and rule order. If the rule
    that claims the file already covers every feature, nothing changes.
    Otherwise an exact-name rule for the file is updated, or a new one is
    inserted just above the rule that claims it today. A new
    `WEB_FEATURES.yml` keeps any features the directory used to inherit.

    Args:
        wpt_root: The WPT checkout.
        test_rel_path: The test file, relative to `wpt_root`.
        feature_ids: Every feature the test should be mapped to.

    Returns:
        What was done, and the features the file is now mapped to.

    Raises:
        WebFeaturesYmlError: If the directory's file can't be edited safely.
    """
    root = wpt_root.resolve()
    rel = Path(test_rel_path)
    directory = root / rel.parent
    yml = directory / WEB_FEATURES_YML
    yml_rel = _rel(yml, root)
    wanted = list(dict.fromkeys(feature_ids))

    if not yml.is_file():
        inherited = inherited_features(root, directory)
        if set(wanted) <= set(inherited):
            return MappingChange(
                rel.as_posix(), yml_rel, ACTION_ALREADY_MAPPED, tuple(inherited)
            )
        text = "rules:\n" + render_rule(rel.name, wanted)
        if inherited:
            text += render_rule(RECURSIVE, inherited)
        yml.write_text(text, encoding="utf-8")
        return MappingChange(
            rel.as_posix(), yml_rel, ACTION_CREATED_FILE, tuple(wanted)
        )

    text = _read(yml)
    rules = parse_rules(text, yml_rel)
    claiming = first_matching_rule(rules, rel.name)
    if claiming and set(wanted) <= set(claiming.feature_ids):
        return MappingChange(
            rel.as_posix(), yml_rel, ACTION_ALREADY_MAPPED, claiming.feature_ids
        )
    anchor = claiming or (rules[0] if rules else None)
    if anchor is None:
        raise WebFeaturesYmlError(f"{yml_rel}: `rules:` is empty")

    lines = text.splitlines(keepends=True)
    if not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    new_rule = render_rule(
        rel.name, wanted, _indent_of(lines[anchor.start_line])
    )
    if anchor.pattern == rel.name:
        lines[anchor.start_line : anchor.end_line] = [new_rule]
        action = ACTION_UPDATED_RULE
    else:
        lines.insert(anchor.start_line, new_rule)
        action = ACTION_ADDED_RULE

    new_text = "".join(lines)
    parse_rules(new_text, yml_rel)
    yml.write_text(new_text, encoding="utf-8")
    return MappingChange(rel.as_posix(), yml_rel, action, tuple(wanted))


_CLASSIFY_SCRIPT = """
import json, sys
sys.path.insert(0, ".")
import tools.localpaths
from tools.manifest.sourcefile import SourceFile
paths = json.load(sys.stdin)
json.dump({p: SourceFile(".", p, "/").manifest_items()[0] for p in paths},
          sys.stdout)
"""


def classify_test_files(wpt_root: Path, rel_paths: list[str]) -> dict[str, str]:
    """Returns each file's WPT test type, using WPT's own `SourceFile`.

    Files that aren't tests (references, helpers, `resources/`) have the
    type `support`.

    Raises:
        WebFeaturesYmlError: If WPT's tools can't be run.
    """
    if not rel_paths:
        return {}
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CLASSIFY_SCRIPT],
            cwd=str(wpt_root),
            input=json.dumps(rel_paths),
            capture_output=True,
            text=True,
            timeout=CLASSIFY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WebFeaturesYmlError(f"Could not classify test files: {e}") from e
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        raise WebFeaturesYmlError(f"Could not classify test files:\n{tail}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise WebFeaturesYmlError(f"Could not classify test files: {e}") from e
    return {str(k): str(v) for k, v in data.items()}
