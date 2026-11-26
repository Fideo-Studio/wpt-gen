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

"""Fixture snapshotting, normalization, injection, and validation CLI."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from benchmark.gap_manifest import load_gap_manifest  # noqa: E402
from benchmark.gap_scoring import (  # noqa: E402
    GapHarnessError,
    assert_run_is_pinned,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPO_ROOT / "benchmarks" / "gap" / "manifest.yaml"


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_files(feature: str) -> tuple[str, str, str]:
    """Return the frozen input filenames required for one feature."""
    return (
        f"resume_{feature}.json",
        "requirements.json",
        "wpt_checkout.json",
    )


def _resolve_fixture_path(path_str: str, repo_root: Path | None) -> Path:
    """Resolve a manifest fixture path, keeping absolute paths unchanged."""
    path = Path(path_str)
    if path.is_absolute() or repo_root is None:
        return path
    return repo_root / path


def _is_relative_wpt_path(path: str) -> bool:
    """Return True for a portable WPT-root-relative POSIX path."""
    if not path or "\\" in path:
        return False
    pure = PurePosixPath(path)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        return False
    first = pure.parts[0]
    return first != "~" and not first.startswith("~") and ":" not in first


def _relativize_path(path: str, wpt_root: Path | None) -> str:
    """Convert a host path under the pinned checkout to a WPT-relative path."""
    if _is_relative_wpt_path(path):
        return PurePosixPath(path).as_posix()
    candidate = Path(path)
    if not candidate.is_absolute():
        raise GapHarnessError(f"invalid WPT-relative path: {path}")
    if wpt_root is None:
        raise GapHarnessError(
            f"cannot relativize absolute path without a WPT root: {path}"
        )
    try:
        relative = candidate.resolve().relative_to(wpt_root.resolve())
    except ValueError as exc:
        raise GapHarnessError(
            f"path is outside the pinned WPT checkout: {path}"
        ) from exc
    return relative.as_posix()


def _absolutize_path(path: str, wpt_root: Path) -> str:
    """Re-root a WPT-relative path against a local checkout."""
    if not _is_relative_wpt_path(path):
        raise GapHarnessError(f"fixture path is not WPT-relative: {path}")
    pure = PurePosixPath(path)
    return str((wpt_root.resolve() / Path(*pure.parts)).resolve())


def _normalize_resume_payload(
    resume: dict[str, Any], wpt_root: Path | None
) -> dict[str, Any]:
    """Rewrite a resume payload so it stores only portable WPT-relative paths."""
    normalized = dict(resume)
    context = dict(normalized.get("wpt_context") or {})
    if "test_contents" in context:
        context["test_contents"] = {
            _relativize_path(key, wpt_root): value
            for key, value in context["test_contents"].items()
        }
    if "dependency_contents" in context:
        context["dependency_contents"] = {
            _relativize_path(key, wpt_root): value
            for key, value in context["dependency_contents"].items()
        }
    if "test_to_deps" in context:
        context["test_to_deps"] = {
            _relativize_path(key, wpt_root): sorted(
                {_relativize_path(dep, wpt_root) for dep in deps}
            )
            for key, deps in context["test_to_deps"].items()
        }
    normalized["wpt_context"] = context
    # The cached manifest carries the author's cache path, checkout root, and
    # build timestamp. Provenance lives in wpt_checkout.json instead.
    normalized.pop("web_features_manifest", None)
    return normalized


def _reroot_resume_payload(
    resume: dict[str, Any], wpt_root: Path
) -> dict[str, Any]:
    """Re-root portable fixture paths against a local WPT checkout."""
    rerooted = dict(resume)
    context = dict(rerooted.get("wpt_context") or {})
    if "test_contents" in context:
        context["test_contents"] = {
            _absolutize_path(key, wpt_root): value
            for key, value in context["test_contents"].items()
        }
    if "dependency_contents" in context:
        context["dependency_contents"] = {
            _absolutize_path(key, wpt_root): value
            for key, value in context["dependency_contents"].items()
        }
    if "test_to_deps" in context:
        context["test_to_deps"] = {
            _absolutize_path(key, wpt_root): [
                _absolutize_path(dep, wpt_root) for dep in deps
            ]
            for key, deps in context["test_to_deps"].items()
        }
    rerooted["wpt_context"] = context
    return rerooted


def _trim_checkout_payload(checkout: dict[str, Any]) -> dict[str, Any]:
    """Keep only machine-independent provenance from wpt_checkout.json."""
    source = checkout.get("checkout", checkout)
    head = source.get("head", source.get("HEAD"))
    if not head:
        raise GapHarnessError("wpt_checkout.json is missing head")
    return {
        "checkout": {
            "head": head,
            "dirty": bool(source.get("dirty", False)),
            "dirty_fingerprint": source.get("dirty_fingerprint"),
        }
    }


def _infer_wpt_root(
    resume: dict[str, Any], checkout: dict[str, Any]
) -> Path | None:
    """Infer the WPT checkout root from legacy fixture metadata."""
    manifest = resume.get("web_features_manifest") or {}
    root = (manifest.get("checkout") or {}).get("root")
    if root:
        return Path(root)
    source = checkout.get("checkout", checkout)
    root = source.get("root")
    return Path(root) if root else None


def _validate_resume_payload(resume_path: Path, location: Path) -> None:
    """Ensure a resume file has non-empty test batch and spec contents."""
    resume: Any = json.loads(resume_path.read_text(encoding="utf-8"))
    wpt_context: Any = resume.get("wpt_context") or {}
    test_contents: Any = wpt_context.get("test_contents")
    if not isinstance(test_contents, dict) or not test_contents:
        raise GapHarnessError(
            f"fixture wpt_context.test_contents is empty: {location}"
        )
    spec_contents: Any = resume.get("spec_contents")
    if not isinstance(spec_contents, dict) or not spec_contents:
        raise GapHarnessError(f"fixture spec_contents is empty: {location}")


def _validate_fixture_resume_paths(
    resume: dict[str, Any], location: Path
) -> None:
    """Reject host paths and embedded manifest metadata in a fixture."""
    context = resume.get("wpt_context") or {}
    paths: list[str] = []
    for name in ("test_contents", "dependency_contents"):
        paths.extend((context.get(name) or {}).keys())
    for key, deps in (context.get("test_to_deps") or {}).items():
        paths.append(key)
        paths.extend(deps)
    invalid = sorted(path for path in paths if not _is_relative_wpt_path(path))
    if invalid:
        raise GapHarnessError(
            f"fixture contains non-relative WPT paths in {location}: "
            f"{invalid[:3]}"
        )
    if "web_features_manifest" in resume:
        raise GapHarnessError(
            f"fixture resume must not embed web_features_manifest: {location}"
        )


def _validate_fixture_checkout(checkout_path: Path) -> None:
    """Reject machine-specific fields in a fixture's provenance file."""
    data: Any = json.loads(checkout_path.read_text(encoding="utf-8"))
    if set(data) != {"checkout"} or not isinstance(data["checkout"], dict):
        raise GapHarnessError(
            "fixture wpt_checkout.json must contain only checkout "
            f"provenance: {checkout_path}"
        )
    checkout = data["checkout"]
    allowed = {"head", "dirty", "dirty_fingerprint"}
    if not set(checkout) <= allowed or "head" not in checkout:
        raise GapHarnessError(
            "fixture wpt_checkout.json has unexpected fields: "
            f"{checkout_path}"
        )
    for value in checkout.values():
        if isinstance(value, str) and (
            "\\" in value or Path(value).is_absolute()
        ):
            raise GapHarnessError(
                "fixture wpt_checkout.json contains an absolute path: "
                f"{checkout_path}"
            )


def validate_fixture_dir(
    fixture_dir: Path,
    feature: str,
    pin: str,
    canary: str,
) -> None:
    """Validate provenance, hashes, and frozen payload of one fixture dir."""
    if not fixture_dir.is_dir():
        raise GapHarnessError(f"missing fixture directory: {fixture_dir}")
    if (fixture_dir / "test_suggestions.json").exists():
        raise GapHarnessError(
            "fixture must not contain test_suggestions.json: " f"{fixture_dir}"
        )

    required_inputs = _fixture_files(feature)
    for name in (*required_inputs, "fixture.json"):
        if not (fixture_dir / name).is_file():
            raise GapHarnessError(
                f"missing required fixture file: {fixture_dir / name}"
            )

    assert_run_is_pinned(fixture_dir, pin, canary)

    fixture_meta: Any = json.loads(
        (fixture_dir / "fixture.json").read_text(encoding="utf-8")
    )
    if fixture_meta.get("feature") != feature:
        raise GapHarnessError(f"fixture.json feature mismatch in {fixture_dir}")
    if fixture_meta.get("wpt_pin") != pin:
        raise GapHarnessError(f"fixture.json wpt_pin mismatch in {fixture_dir}")
    sha_map: Any = fixture_meta.get("sha256")
    if not isinstance(sha_map, dict):
        raise GapHarnessError(f"fixture.json missing sha256 in {fixture_dir}")
    for name in required_inputs:
        expected = sha_map.get(name)
        actual = _sha256(fixture_dir / name)
        if expected != actual:
            raise GapHarnessError(f"sha256 mismatch for {fixture_dir / name}")

    resume_path = fixture_dir / f"resume_{feature}.json"
    _validate_resume_payload(resume_path, fixture_dir)
    resume: Any = json.loads(resume_path.read_text(encoding="utf-8"))
    _validate_fixture_resume_paths(resume, fixture_dir)
    _validate_fixture_checkout(fixture_dir / "wpt_checkout.json")


def validate_manifest_fixtures(
    manifest_path: Path,
    *,
    repo_root: Path | None = None,
    require_all: bool = True,
) -> list[Path]:
    """Validate fixtures declared in a gap manifest."""
    pin, canary, _, fixtures = load_gap_manifest(manifest_path)
    if not canary:
        raise GapHarnessError("manifest canary is missing")

    validated: list[Path] = []
    for feature, entry in fixtures.items():
        fixture_dir = _resolve_fixture_path(entry.path, repo_root)
        if not fixture_dir.exists():
            if require_all:
                raise GapHarnessError(
                    f"missing fixture directory: {fixture_dir}"
                )
            continue
        validate_fixture_dir(fixture_dir, feature, pin, canary)
        validated.append(fixture_dir)

    if not validated:
        raise GapHarnessError("no committed fixtures found to validate")
    return validated


def snapshot_fixture(
    feature: str,
    wpt_pin_dir: Path,
    manifest_path: Path,
    tmp_dir: Path,
    *,
    config_path: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Freeze one feature's inputs from the pinned WPT checkout."""
    pin, canary, _, fixtures = load_gap_manifest(manifest_path)
    if feature not in fixtures:
        raise GapHarnessError(f"feature not listed in manifest: {feature}")
    if not canary:
        raise GapHarnessError("manifest canary is missing")

    tmp_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "wpt-gen",
        "generate",
        feature,
        "--wpt-dir",
        str(wpt_pin_dir),
        "--suggestions-only",
        "--state-dir",
        str(tmp_dir),
        "--no-cache",
        "--yes-tokens",
    ]
    if config_path is not None:
        cmd.extend(["--config", str(config_path)])
    subprocess.run(
        cmd,
        check=True,
        timeout=600,
        env={**os.environ},
    )

    required_inputs = _fixture_files(feature)
    for name in required_inputs:
        if not (tmp_dir / name).is_file():
            raise GapHarnessError(f"snapshot did not produce {name}: {tmp_dir}")
    assert_run_is_pinned(tmp_dir, pin)
    resume_src = tmp_dir / f"resume_{feature}.json"
    _validate_resume_payload(resume_src, tmp_dir)

    resume: Any = json.loads(resume_src.read_text(encoding="utf-8"))
    normalized_resume = _normalize_resume_payload(resume, wpt_pin_dir)
    checkout: Any = json.loads(
        (tmp_dir / "wpt_checkout.json").read_text(encoding="utf-8")
    )
    trimmed_checkout = _trim_checkout_payload(checkout)

    dest = _resolve_fixture_path(fixtures[feature].path, repo_root)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"resume_{feature}.json").write_text(
        json.dumps(normalized_resume, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copy2(tmp_dir / "requirements.json", dest / "requirements.json")
    (dest / "wpt_checkout.json").write_text(
        json.dumps(trimmed_checkout, indent=2) + "\n", encoding="utf-8"
    )
    hashes = {name: _sha256(dest / name) for name in required_inputs}

    fixture_meta = {
        "feature": feature,
        "canary": canary,
        "wpt_pin": pin,
        "sha256": hashes,
    }
    (dest / "fixture.json").write_text(
        json.dumps(fixture_meta, indent=2) + "\n", encoding="utf-8"
    )
    validate_fixture_dir(dest, feature, pin, canary)
    return dest


def inject_fixture(
    feature: str,
    fixture_dir: Path,
    wpt_pin_dir: Path,
    rep_dir: Path,
    *,
    pin: str,
    canary: str,
) -> Path:
    """Copy a frozen fixture into a fresh repeat dir and re-root its paths."""
    validate_fixture_dir(fixture_dir, feature, pin, canary)
    if rep_dir.exists():
        raise GapHarnessError(f"repeat directory already exists: {rep_dir}")
    rep_dir.mkdir(parents=True)

    for name in (*_fixture_files(feature), "fixture.json"):
        shutil.copy2(fixture_dir / name, rep_dir / name)

    resume_path = rep_dir / f"resume_{feature}.json"
    resume: Any = json.loads(resume_path.read_text(encoding="utf-8"))
    rerooted = _reroot_resume_payload(resume, wpt_pin_dir)
    resume_path.write_text(
        json.dumps(rerooted, indent=2) + "\n", encoding="utf-8"
    )
    assert_run_is_pinned(rep_dir, pin, canary)
    return rep_dir


def normalize_fixture_dir(
    fixture_dir: Path,
    feature: str,
    *,
    pin: str,
    canary: str,
    wpt_root: Path | None = None,
) -> Path:
    """Rewrite one fixture's stored paths to portable WPT-relative form."""
    if not fixture_dir.is_dir():
        raise GapHarnessError(f"missing fixture directory: {fixture_dir}")
    required_inputs = _fixture_files(feature)
    for name in required_inputs:
        if not (fixture_dir / name).is_file():
            raise GapHarnessError(
                f"missing required fixture file: {fixture_dir / name}"
            )

    existing_meta_path = fixture_dir / "fixture.json"
    if existing_meta_path.is_file():
        existing_meta: Any = json.loads(
            existing_meta_path.read_text(encoding="utf-8")
        )
        if existing_meta.get("feature") not in (None, feature):
            raise GapHarnessError(
                f"fixture.json feature mismatch in {fixture_dir}"
            )
        if existing_meta.get("wpt_pin") not in (None, pin):
            raise GapHarnessError(
                f"fixture.json wpt_pin mismatch in {fixture_dir}"
            )
        if existing_meta.get("canary") not in (None, canary):
            raise GapHarnessError(
                f"fixture.json canary mismatch in {fixture_dir}"
            )

    resume_path = fixture_dir / f"resume_{feature}.json"
    checkout_path = fixture_dir / "wpt_checkout.json"
    resume: Any = json.loads(resume_path.read_text(encoding="utf-8"))
    checkout: Any = json.loads(checkout_path.read_text(encoding="utf-8"))
    root = wpt_root or _infer_wpt_root(resume, checkout)

    normalized = _normalize_resume_payload(resume, root)
    trimmed = _trim_checkout_payload(checkout)
    resume_path.write_text(
        json.dumps(normalized, indent=2) + "\n", encoding="utf-8"
    )
    checkout_path.write_text(
        json.dumps(trimmed, indent=2) + "\n", encoding="utf-8"
    )

    hashes = {name: _sha256(fixture_dir / name) for name in required_inputs}
    fixture_meta = {
        "feature": feature,
        "canary": canary,
        "wpt_pin": pin,
        "sha256": hashes,
    }
    (fixture_dir / "fixture.json").write_text(
        json.dumps(fixture_meta, indent=2) + "\n", encoding="utf-8"
    )
    validate_fixture_dir(fixture_dir, feature, pin, canary)
    return fixture_dir


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for gap fixture tooling."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snap = subparsers.add_parser(
        "snapshot",
        help="Snapshot one feature or all missing manifest features.",
    )
    snap.add_argument("feature", nargs="?", help="Feature ID to snapshot.")
    snap.add_argument(
        "--all-missing",
        action="store_true",
        help="Snapshot every manifest feature without an existing fixture.",
    )
    snap.add_argument(
        "--wpt-dir",
        type=Path,
        required=True,
        help="Path to the clean pinned WPT worktree.",
    )
    snap.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to benchmarks/gap/manifest.yaml.",
    )
    snap.add_argument(
        "--tmp-dir",
        type=Path,
        default=None,
        help="Optional working state directory for the snapshot run.",
    )
    snap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Optional wpt-gen.yml path passed through to wpt-gen.",
    )

    norm = subparsers.add_parser(
        "normalize",
        help="Rewrite committed fixtures to portable WPT-relative paths.",
    )
    norm.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to benchmarks/gap/manifest.yaml.",
    )
    norm.add_argument(
        "--allow-missing",
        action="store_true",
        help="Normalize only existing fixture directories.",
    )
    norm.add_argument(
        "--wpt-dir",
        type=Path,
        default=None,
        help="Optional WPT root for legacy fixtures without recorded roots.",
    )

    verify = subparsers.add_parser(
        "verify",
        help="Validate committed fixtures against the gap manifest.",
    )
    verify.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to benchmarks/gap/manifest.yaml.",
    )
    verify.add_argument(
        "--allow-missing",
        action="store_true",
        help="Validate only committed fixtures and skip absent ones.",
    )
    return parser


def _snapshot_batch(
    features: list[str],
    args: argparse.Namespace,
    tmp_root: Path | None,
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Snapshot a list of features; continue on failure in batch mode."""
    written: list[Path] = []
    failures: list[tuple[str, str]] = []
    for feat in features:
        if tmp_root is not None:
            run_tmp = tmp_root / feat
        elif args.all_missing:
            run_tmp = args.tmp_dir / feat
        else:
            run_tmp = args.tmp_dir
        try:
            dest = snapshot_fixture(
                feat,
                args.wpt_dir,
                args.manifest,
                run_tmp,
                config_path=args.config,
                repo_root=REPO_ROOT,
            )
            written.append(dest)
            sys.stdout.write(f"Wrote fixture: {dest}\n")
        except (
            GapHarnessError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            if not args.all_missing:
                raise GapHarnessError(str(exc)) from exc
            failures.append((feat, str(exc)))
            sys.stderr.write(f"failed {feat}: {exc}\n")
    return written, failures


def _run_snapshot_command(
    args: argparse.Namespace,
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Execute the snapshot CLI subcommand."""
    if bool(args.feature) == bool(args.all_missing):
        raise GapHarnessError(
            "provide either <feature> or --all-missing, not both"
        )

    if args.feature:
        features = [str(args.feature)]
    else:
        _, _, _, fixtures = load_gap_manifest(args.manifest)
        features = [
            feat
            for feat, entry in fixtures.items()
            if not _resolve_fixture_path(entry.path, REPO_ROOT).exists()
        ]
        if not features:
            return [], []

    if args.tmp_dir is not None:
        return _snapshot_batch(features, args, tmp_root=None)

    with tempfile.TemporaryDirectory(prefix="gap-snapshot-") as temp_root:
        return _snapshot_batch(features, args, tmp_root=Path(temp_root))


def _run_normalize_command(args: argparse.Namespace) -> list[Path]:
    """Execute the normalize CLI subcommand."""
    pin, canary, _, fixtures = load_gap_manifest(args.manifest)
    if not canary:
        raise GapHarnessError("manifest canary is missing")

    normalized: list[Path] = []
    for feature, entry in fixtures.items():
        fixture_dir = _resolve_fixture_path(entry.path, REPO_ROOT)
        if not fixture_dir.exists():
            if args.allow_missing:
                continue
            raise GapHarnessError(f"missing fixture directory: {fixture_dir}")
        normalize_fixture_dir(
            fixture_dir,
            feature,
            pin=pin,
            canary=canary,
            wpt_root=args.wpt_dir,
        )
        normalized.append(fixture_dir)
        sys.stdout.write(f"Normalized fixture: {fixture_dir}\n")
    if not normalized:
        raise GapHarnessError("no fixtures found to normalize")
    return normalized


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for snapshotting, normalizing, and verifying fixtures."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            written, failures = _run_snapshot_command(args)
            if not written and not failures:
                sys.stdout.write("All manifest fixtures already exist.\n")
            if failures:
                summary = ", ".join(feat for feat, _ in failures)
                sys.stderr.write(
                    f"Snapshot failed for {len(failures)} feature(s): "
                    f"{summary}\n"
                )
                return 1
            return 0
        if args.command == "normalize":
            normalized = _run_normalize_command(args)
            sys.stdout.write(
                f"Normalized {len(normalized)} gap fixture(s) from "
                f"{args.manifest}\n"
            )
            return 0
        validated = validate_manifest_fixtures(
            args.manifest,
            repo_root=REPO_ROOT,
            require_all=not args.allow_missing,
        )
        sys.stdout.write(
            f"Validated {len(validated)} gap fixture(s) from {args.manifest}\n"
        )
        return 0
    except GapHarnessError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
