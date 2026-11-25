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

"""Tests for resolving WPT's web-features manifest."""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from wptgen.models import WorkflowContext
from wptgen.web_features_manifest import (
    CACHE_SUBDIR,
    KEEP_CACHED_MANIFESTS,
    ResolvedManifest,
    WebFeaturesManifestError,
    WPTCheckout,
    _prune,
    build_manifest_at,
    feature_test_paths,
    features_for_paths,
    inspect_checkout,
    load_manifest_data,
    resolve_manifest,
    url_to_source_path,
)

BUILT_DATA = {"grid": ["/css/grid.html"], "built": ["/built.html"]}

# Fake `./wpt`: writes BUILT_DATA to the -p path and logs each call.
FAKE_WPT_SCRIPT = """#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("FAKE_WPT_LOG")
if log:
    with open(log, "a") as f:
        f.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_WPT_FAIL"):
    sys.stderr.write("boom: virtualenv setup failed\\n")
    sys.exit(3)
version = int(os.environ.get("FAKE_WPT_VERSION", "1"))
path = sys.argv[sys.argv.index("-p") + 1]
with open(path, "w") as f:
    json.dump({"version": version, "data": __DATA__}, f)
""".replace("__DATA__", json.dumps(BUILT_DATA))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def wpt_repo(tmp_path: Path) -> Path:
    """A minimal WPT-like git checkout."""
    repo = tmp_path / "wpt"
    (repo / "css").mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "css" / "grid.html").write_text("<title>grid</title>")
    wpt_script = repo / "wpt"
    wpt_script.write_text(FAKE_WPT_SCRIPT)
    wpt_script.chmod(0o755)
    _commit(repo, "initial")
    return repo


@pytest.fixture
def build_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "wpt-invocations.log"
    monkeypatch.setenv("FAKE_WPT_LOG", str(log))
    return log


def _build_count(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


@pytest.mark.parametrize(
    ("url", "source"),
    [
        ("/a/test.html", "a/test.html"),
        ("/a/test.html?variant=1", "a/test.html"),
        ("/a/test.html#frag", "a/test.html"),
        ("/a/api.any.html", "a/api.any.js"),
        ("/a/api.any.worker.html", "a/api.any.js"),
        ("/a/api.any.sharedworker.html?x=1", "a/api.any.js"),
        ("/a/api.https.any.serviceworker.html", "a/api.https.any.js"),
        ("/a/win.window.html", "a/win.window.js"),
        ("/a/w.worker.html", "a/w.worker.js"),
        ("/a/ext.extension.html", "a/ext.extension.js"),
        ("/root.window.html", "root.window.js"),
        ("/webdriver/tests/x.py", "webdriver/tests/x.py"),
    ],
)
def test_url_to_source_path(tmp_path: Path, url: str, source: str) -> None:
    target = tmp_path / source
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()

    assert url_to_source_path(url, tmp_path) == target


def test_url_to_source_path_prefers_existing_html(tmp_path: Path) -> None:
    """A real .worker.html file wins over the .worker.js mapping."""
    (tmp_path / "x.worker.html").touch()
    (tmp_path / "x.worker.js").touch()

    assert url_to_source_path("/x.worker.html", tmp_path) == (
        tmp_path / "x.worker.html"
    )


@pytest.mark.parametrize(
    "url", ["/missing.html", "/missing.any.html", "/missing.window.html", "/"]
)
def test_url_to_source_path_missing(tmp_path: Path, url: str) -> None:
    assert url_to_source_path(url, tmp_path) is None


def test_feature_test_paths(tmp_path: Path) -> None:
    (tmp_path / "a.any.js").touch()
    (tmp_path / "b.html").touch()
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "data": {
                    "feat": [
                        "/a.any.html",
                        "/a.any.worker.html",
                        "/b.html",
                        "/gone.html",
                    ]
                },
            }
        )
    )

    paths, unmapped = feature_test_paths(manifest, tmp_path, "feat")

    root = tmp_path.resolve()
    assert paths == [str(root / "a.any.js"), str(root / "b.html")]
    assert unmapped == ["/gone.html"]
    assert feature_test_paths(manifest, tmp_path, "absent") == ([], [])


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[]", "not a JSON object"),
        ('{"version": 2, "data": {}}', "version 2"),
        ('{"version": 1}', "no valid 'data' map"),
        ('{"version": 1, "data": {"f": "not-a-list"}}', "no valid 'data' map"),
        ("{not json", "Could not read"),
    ],
)
def test_load_manifest_data_rejects_bad_files(
    tmp_path: Path, content: str, message: str
) -> None:
    manifest = tmp_path / "m.json"
    manifest.write_text(content)

    with pytest.raises(WebFeaturesManifestError, match=message):
        load_manifest_data(manifest)


def test_load_manifest_data_missing_file(tmp_path: Path) -> None:
    with pytest.raises(WebFeaturesManifestError, match="not found"):
        load_manifest_data(tmp_path / "absent.json")


def test_load_manifest_data_rereads_changed_file(tmp_path: Path) -> None:
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"version": 1, "data": {"a": []}}))
    assert list(load_manifest_data(manifest)) == ["a"]

    manifest.write_text(json.dumps({"version": 1, "data": {"b": []}}))
    future = time.time() + 10
    os.utime(manifest, (future, future))
    assert list(load_manifest_data(manifest)) == ["b"]


def test_inspect_checkout_clean(wpt_repo: Path) -> None:
    head = _git(wpt_repo, "rev-parse", "HEAD")

    checkout = inspect_checkout(wpt_repo)

    assert checkout.head == head
    assert not checkout.dirty
    assert checkout.dirty_fingerprint is None
    assert checkout.cache_key == head
    assert checkout.describe() == f"HEAD {head[:10]}"


def test_inspect_checkout_dirty_states(wpt_repo: Path) -> None:
    grid = wpt_repo / "css" / "grid.html"
    grid.write_text("changed once")
    first = inspect_checkout(wpt_repo)

    assert first.dirty
    assert first.dirty_fingerprint
    assert first.cache_key.startswith(f"{first.head}-dirty-")
    assert first.describe().endswith(", with uncommitted changes")

    grid.write_text("changed twice")
    second = inspect_checkout(wpt_repo)
    assert second.cache_key != first.cache_key

    _git(wpt_repo, "checkout", "--", "css/grid.html")
    (wpt_repo / "css" / "untracked.html").write_text("untracked")
    third = inspect_checkout(wpt_repo)
    assert third.dirty
    assert third.cache_key not in (first.cache_key, second.cache_key)


def test_inspect_checkout_not_a_git_checkout(tmp_path: Path) -> None:
    with pytest.raises(WebFeaturesManifestError, match="not a git checkout"):
        inspect_checkout(tmp_path)


def test_resolve_builds_and_caches_per_commit(
    wpt_repo: Path, tmp_path: Path, build_log: Path
) -> None:
    notify = MagicMock()
    head = _git(wpt_repo, "rev-parse", "HEAD")

    resolved = resolve_manifest(wpt_repo, tmp_path / "cache", notify=notify)

    assert resolved.checkout.head == head
    assert Path(resolved.path).name == f"{head}.json"
    assert load_manifest_data(resolved.path) == BUILT_DATA
    assert build_log.read_text().startswith("web-features-manifest -p ")
    notify.assert_called_once()

    # Same checkout state, so the cache is reused.
    again = resolve_manifest(wpt_repo, tmp_path / "cache")
    assert again == resolved
    assert _build_count(build_log) == 1

    # A new commit is a new checkout state.
    (wpt_repo / "css" / "local.html").write_text("local")
    new_head = _commit(wpt_repo, "local mapping fix")
    rebuilt = resolve_manifest(wpt_repo, tmp_path / "cache")
    assert Path(rebuilt.path).name == f"{new_head}.json"
    assert _build_count(build_log) == 2


def test_resolve_builds_again_for_each_dirty_state(
    wpt_repo: Path, tmp_path: Path, build_log: Path
) -> None:
    (wpt_repo / "css" / "grid.html").write_text("edited")
    first = resolve_manifest(wpt_repo, tmp_path / "cache")
    (wpt_repo / "css" / "grid.html").write_text("edited again")
    second = resolve_manifest(wpt_repo, tmp_path / "cache")

    assert first.checkout.dirty
    assert second.checkout.dirty
    assert first.path != second.path
    assert Path(first.path).is_file()
    assert _build_count(build_log) == 2


def test_resolve_rebuild_ignores_cache(
    wpt_repo: Path, tmp_path: Path, build_log: Path
) -> None:
    resolve_manifest(wpt_repo, tmp_path / "cache")
    resolve_manifest(wpt_repo, tmp_path / "cache", rebuild=True)

    assert _build_count(build_log) == 2


def test_resolve_build_failure_stops_with_wpt_output(
    wpt_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_WPT_FAIL", "1")

    with pytest.raises(WebFeaturesManifestError) as excinfo:
        resolve_manifest(wpt_repo, tmp_path / "cache")

    assert "exit code 3" in str(excinfo.value)
    assert "boom: virtualenv setup failed" in str(excinfo.value)
    assert list((tmp_path / "cache" / CACHE_SUBDIR).iterdir()) == []


def test_resolve_rejects_unknown_manifest_version(
    wpt_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_WPT_VERSION", "2")

    with pytest.raises(WebFeaturesManifestError, match="version 2"):
        resolve_manifest(wpt_repo, tmp_path / "cache")


def test_resolve_build_timeout(
    wpt_repo: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    real_run = subprocess.run

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        if cmd[0] == "./wpt":
            raise subprocess.TimeoutExpired(cmd, 1)
        return real_run(cmd, **kwargs)

    mocker.patch(
        "wptgen.web_features_manifest.subprocess.run", side_effect=fake_run
    )

    with pytest.raises(WebFeaturesManifestError, match="timed out"):
        resolve_manifest(wpt_repo, tmp_path / "cache")


def test_resolve_without_wpt_script(wpt_repo: Path, tmp_path: Path) -> None:
    (wpt_repo / "wpt").unlink()

    with pytest.raises(WebFeaturesManifestError, match="No `wpt` script"):
        resolve_manifest(wpt_repo, tmp_path / "cache")


def test_resolve_replaces_corrupt_cache_entry(
    wpt_repo: Path, tmp_path: Path, build_log: Path
) -> None:
    first = resolve_manifest(wpt_repo, tmp_path / "cache")
    Path(first.path).write_text("{corrupt")

    second = resolve_manifest(wpt_repo, tmp_path / "cache")

    assert second.path == first.path
    assert load_manifest_data(second.path) == BUILT_DATA
    assert _build_count(build_log) == 2


def test_prune_keeps_most_recent_manifests(tmp_path: Path) -> None:
    for i in range(KEEP_CACHED_MANIFESTS + 3):
        manifest = tmp_path / f"{i}.json"
        manifest.write_text("{}")
        (tmp_path / f"{i}.meta.json").write_text("{}")
        os.utime(manifest, (1000 + i, 1000 + i))
    keep = tmp_path / "0.json"

    _prune(tmp_path, keep=keep)

    remaining = sorted(
        p.name for p in tmp_path.glob("*.json") if ".meta." not in p.name
    )
    newest = [f"{i}.json" for i in range(3, KEEP_CACHED_MANIFESTS + 3)]
    assert remaining == sorted(["0.json", *newest])
    assert not (tmp_path / "1.meta.json").exists()
    assert (tmp_path / "0.meta.json").exists()


def _resolved() -> ResolvedManifest:
    return ResolvedManifest(
        path="/cache/abc.json",
        created_at="2026-11-16T00:00:00+00:00",
        checkout=WPTCheckout(
            root="/wpt",
            head="abc123",
            dirty=True,
            dirty_fingerprint="f" * 64,
        ),
    )


def test_resolved_manifest_round_trip() -> None:
    resolved = _resolved()

    assert ResolvedManifest.from_dict(resolved.to_dict()) == resolved
    assert resolved.describe() == (
        "built for HEAD abc123, with uncommitted changes"
    )


def test_workflow_context_round_trips_manifest() -> None:
    context = WorkflowContext(
        feature_id="grid", web_features_manifest=_resolved()
    )

    restored = WorkflowContext.from_dict(
        json.loads(json.dumps(context.to_dict()))
    )

    assert restored.web_features_manifest == _resolved()
    assert (
        WorkflowContext.from_dict({"feature_id": "x"}).web_features_manifest
        is None
    )


def test_features_for_paths(tmp_path: Path) -> None:
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "data": {
                    "grid": ["/css/a.html", "/api/x.any.html"],
                    "subgrid": [
                        "/api/x.any.worker.html?v=1",
                        "/w/y.window.html",
                    ],
                },
            }
        )
    )
    assert features_for_paths(
        manifest, ["css/a.html", "api/x.any.js", "w/y.window.js", "none.html"]
    ) == {
        "css/a.html": ["grid"],
        "api/x.any.js": ["grid", "subgrid"],
        "w/y.window.js": ["subgrid"],
        "none.html": [],
    }


def test_build_manifest_at_writes_uncached(
    wpt_repo: Path, tmp_path: Path, build_log: Path
) -> None:
    dest = build_manifest_at(wpt_repo, tmp_path / "out" / "after.json")
    assert dest.is_file()
    assert load_manifest_data(dest) == BUILT_DATA
    assert _build_count(build_log) == 1
