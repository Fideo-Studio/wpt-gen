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

"""Resolution and caching of WPT's web-features manifest."""

import hashlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1
CACHE_SUBDIR = "web-features-manifest"
# Generous, since the first ./wpt run bootstraps WPT's virtualenv.
BUILD_TIMEOUT_SECONDS = 1800
GIT_TIMEOUT_SECONDS = 120
KEEP_CACHED_MANIFESTS = 5
ERROR_OUTPUT_LINES = 40

# URL suffixes of tests generated from a .js file (".any." is handled apart).
_JS_SOURCE_URL_SUFFIXES = (".window.html", ".worker.html", ".extension.html")


class WebFeaturesManifestError(RuntimeError):
    """Raised when the web-features manifest cannot be obtained or read."""


@dataclass(frozen=True)
class WPTCheckout:
    """State of a local WPT checkout."""

    root: str
    head: str
    dirty: bool
    dirty_fingerprint: str | None = None

    @property
    def cache_key(self) -> str:
        """Cache key for this checkout state."""
        if self.dirty and self.dirty_fingerprint:
            return f"{self.head}-dirty-{self.dirty_fingerprint[:16]}"
        return self.head

    def describe(self) -> str:
        """Returns a short summary of the checkout state."""
        base = f"HEAD {self.head[:10]}"
        if self.dirty:
            base += ", with uncommitted changes"
        return base

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WPTCheckout":
        return cls(
            root=data["root"],
            head=data["head"],
            dirty=data.get("dirty", False),
            dirty_fingerprint=data.get("dirty_fingerprint"),
        )


@dataclass(frozen=True)
class ResolvedManifest:
    """A cached web-features manifest and the checkout it was made for."""

    path: str
    created_at: str
    checkout: WPTCheckout

    def describe(self) -> str:
        return f"built for {self.checkout.describe()}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["checkout"] = self.checkout.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolvedManifest":
        return cls(
            path=data["path"],
            created_at=data["created_at"],
            checkout=WPTCheckout.from_dict(data["checkout"]),
        )


def _git(wpt_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(wpt_root), *args],
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _git_text(wpt_root: Path, *args: str) -> str | None:
    result = _git(wpt_root, *args)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace").strip()


def _dirty_fingerprint(wpt_root: Path, status: bytes) -> str:
    """Hashes uncommitted changes, including untracked files."""
    digest = hashlib.sha256()
    digest.update(status)
    diff = _git(wpt_root, "diff", "HEAD", "--binary")
    digest.update(diff.stdout)

    entries = [e for e in status.split(b"\0") if e]
    for entry in entries:
        if not entry.startswith(b"?? "):
            continue
        rel_path = entry[3:].decode("utf-8", errors="replace")
        try:
            stat = (wpt_root / rel_path).stat()
        except OSError:
            continue
        digest.update(f"{rel_path}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def inspect_checkout(wpt_root: Path) -> WPTCheckout:
    """Reads the HEAD commit and dirty state of a checkout."""
    root = wpt_root.resolve()
    head = _git_text(root, "rev-parse", "HEAD")
    if not head:
        raise WebFeaturesManifestError(
            f"{root} is not a git checkout (or git is unavailable). wpt-gen "
            "needs a git checkout of WPT to resolve its web-features manifest."
        )

    status = _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
    )
    if status.returncode != 0:
        stderr = status.stderr.decode("utf-8", errors="replace").strip()
        raise WebFeaturesManifestError(
            f"`git status` failed in {root}: {stderr}"
        )
    dirty = bool(status.stdout.strip(b"\0"))

    return WPTCheckout(
        root=str(root),
        head=head,
        dirty=dirty,
        dirty_fingerprint=(
            _dirty_fingerprint(root, status.stdout) if dirty else None
        ),
    )


_DATA_CACHE: dict[tuple[str, int], dict[str, list[str]]] = {}


def _validate_manifest(raw: Any, source: str) -> dict[str, list[str]]:
    """Validates a parsed manifest and returns its feature -> URLs map."""
    if not isinstance(raw, dict):
        raise WebFeaturesManifestError(
            f"Web-features manifest from {source} is not a JSON object."
        )
    version = raw.get("version")
    if version != MANIFEST_VERSION:
        raise WebFeaturesManifestError(
            f"Web-features manifest from {source} has version {version!r}; "
            f"wpt-gen understands version {MANIFEST_VERSION}. WPT may have "
            "changed the manifest format."
        )
    data = raw.get("data")
    if not isinstance(data, dict) or not all(
        isinstance(urls, list) for urls in data.values()
    ):
        raise WebFeaturesManifestError(
            f"Web-features manifest from {source} has no valid 'data' map."
        )
    return data


def load_manifest_data(path: str | Path) -> dict[str, list[str]]:
    """Loads a manifest's feature -> URLs map, memoised by path and mtime."""
    manifest_path = Path(path)
    try:
        mtime = manifest_path.stat().st_mtime_ns
    except OSError as e:
        raise WebFeaturesManifestError(
            f"Web-features manifest not found at {manifest_path}: {e}"
        ) from e

    key = (str(manifest_path), mtime)
    if key not in _DATA_CACHE:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise WebFeaturesManifestError(
                f"Could not read web-features manifest {manifest_path}: {e}"
            ) from e
        _DATA_CACHE.clear()
        _DATA_CACHE[key] = _validate_manifest(raw, str(manifest_path))
    return _DATA_CACHE[key]


def _tail(text: str, lines: int = ERROR_OUTPUT_LINES) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def _build_manifest(wpt_root: Path, dest: Path) -> None:
    """Builds the manifest with `./wpt web-features-manifest`."""
    wpt_script = wpt_root / "wpt"
    if not wpt_script.is_file():
        raise WebFeaturesManifestError(
            f"No `wpt` script found in {wpt_root}; is this a WPT checkout?"
        )
    try:
        result = subprocess.run(
            ["./wpt", "web-features-manifest", "-p", str(dest)],
            cwd=str(wpt_root),
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise WebFeaturesManifestError(
            "`./wpt web-features-manifest` timed out after "
            f"{BUILD_TIMEOUT_SECONDS}s in {wpt_root}."
        ) from e
    except OSError as e:
        raise WebFeaturesManifestError(
            f"Could not run `./wpt web-features-manifest` in {wpt_root}: {e}"
        ) from e

    if result.returncode != 0:
        output = _tail(f"{result.stdout}\n{result.stderr}")
        raise WebFeaturesManifestError(
            "`./wpt web-features-manifest` failed with exit code "
            f"{result.returncode} in {wpt_root}:\n{output}"
        )
    if not dest.is_file():
        raise WebFeaturesManifestError(
            "`./wpt web-features-manifest` exited successfully but wrote no "
            f"manifest to {dest}."
        )
    try:
        raw = json.loads(dest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise WebFeaturesManifestError(
            f"`./wpt web-features-manifest` wrote an unreadable manifest: {e}"
        ) from e
    _validate_manifest(raw, "`./wpt web-features-manifest`")


def _prune(cache_dir: Path, keep: Path) -> None:
    """Deletes all but the most recent cached manifests."""
    manifests = sorted(
        (
            p
            for p in cache_dir.glob("*.json")
            if not p.name.endswith(".meta.json")
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in manifests[KEEP_CACHED_MANIFESTS:]:
        if stale == keep:
            continue
        stale.unlink(missing_ok=True)
        stale.with_suffix(".meta.json").unlink(missing_ok=True)


def _load_cached(
    manifest_path: Path, meta_path: Path
) -> ResolvedManifest | None:
    """Returns the cached manifest, or None if missing or invalid."""
    if not (manifest_path.is_file() and meta_path.is_file()):
        return None
    try:
        meta = ResolvedManifest.from_dict(
            json.loads(meta_path.read_text(encoding="utf-8"))
        )
        load_manifest_data(manifest_path)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as e:
        logger.warning(f"Ignoring unreadable cached manifest {meta_path}: {e}")
        return None
    except WebFeaturesManifestError as e:
        logger.warning(f"Ignoring invalid cached manifest: {e}")
        return None
    return meta


def resolve_manifest(
    wpt_root: str | Path,
    cache_dir: str | Path,
    *,
    rebuild: bool = False,
    notify: Callable[[str], None] | None = None,
) -> ResolvedManifest:
    """Returns the web-features manifest for the checkout's current state.

    Uses the cache if possible, otherwise builds one with
    `./wpt web-features-manifest`.

    Args:
        wpt_root: The WPT checkout.
        cache_dir: wpt-gen's cache directory.
        rebuild: Ignore any cached manifest.
        notify: Receives progress messages before slow steps.

    Raises:
        WebFeaturesManifestError: If no manifest can be obtained.
    """
    root = Path(wpt_root).resolve()
    checkout = inspect_checkout(root)

    manifest_dir = Path(cache_dir) / CACHE_SUBDIR
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / f"{checkout.cache_key}.json"
    meta_path = manifest_dir / f"{checkout.cache_key}.meta.json"

    if not rebuild:
        cached = _load_cached(manifest_path, meta_path)
        if cached is not None:
            return cached

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{checkout.cache_key}.", suffix=".tmp", dir=manifest_dir
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        if notify:
            notify("Building WPT's web-features manifest (about a minute)...")
        _build_manifest(root, tmp_path)
        os.replace(tmp_path, manifest_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    resolved = ResolvedManifest(
        path=str(manifest_path),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        checkout=checkout,
    )
    meta_tmp = meta_path.with_suffix(f".{os.getpid()}.tmp")
    meta_tmp.write_text(
        json.dumps(resolved.to_dict(), indent=2), encoding="utf-8"
    )
    os.replace(meta_tmp, meta_path)

    _prune(manifest_dir, keep=manifest_path)
    return resolved


def url_to_source_path(url: str, wpt_root: Path) -> Path | None:
    """Maps a test URL (e.g. `/a/b.any.worker.html`) to its source file."""
    rel = url.split("#", 1)[0].split("?", 1)[0].lstrip("/")
    if not rel:
        return None

    direct = wpt_root / rel
    if direct.is_file():
        return direct

    directory, _, filename = rel.rpartition("/")
    candidate_name = None
    if ".any." in filename:
        candidate_name = (
            filename[: filename.index(".any.") + len(".any.")] + "js"
        )
    else:
        for suffix in _JS_SOURCE_URL_SUFFIXES:
            if filename.endswith(suffix):
                candidate_name = filename[: -len(".html")] + ".js"
                break

    if candidate_name is None:
        return None
    candidate = (
        wpt_root / directory / candidate_name
        if directory
        else (wpt_root / candidate_name)
    )
    return candidate if candidate.is_file() else None


def feature_test_paths(
    manifest_path: str | Path, wpt_root: str | Path, feature_id: str
) -> tuple[list[str], list[str]]:
    """Returns a feature's source paths and any URLs with no source file."""
    root = Path(wpt_root).resolve()
    urls = load_manifest_data(manifest_path).get(feature_id, [])
    paths: set[str] = set()
    unmapped: list[str] = []
    for url in urls:
        source = url_to_source_path(url, root)
        if source is None:
            unmapped.append(url)
        else:
            paths.add(str(source))
    return sorted(paths), unmapped
