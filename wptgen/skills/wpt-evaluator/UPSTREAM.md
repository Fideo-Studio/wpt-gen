# Upstream provenance

The rules in `references/rules.yaml` are currently derived from the `docs/`
tree of the upstream `web-platform-tests/wpt` repository. Rules from
wpt-gen's own style guides may be added in a later pass; provenance is
tracked per rule via the `source` field.

## Source path convention

The `source` field on each rule is a repo-root-relative path whose first
segment names the originating repository:

- `wpt/...` — paths into upstream `web-platform-tests/wpt` (e.g.,
  `wpt/docs/writing-tests/general-guidelines.md#L82-L87`).
- `wpt-gen/...` — paths into this repository (e.g.,
  `wpt-gen/wptgen/skills/wpt-generator/references/testharness_style_guide.md#L124`).

A `#L<start>-L<end>` line anchor is appended where the source location is
stable.

## Pinned upstream commit

The commit these rules were checked against is recorded once, as
`wpt_upstream_commit` in [`references/rules.yaml`](references/rules.yaml).
The evaluator benchmark's corpus is pinned to the same commit
(`benchmarks/manifest.yaml`); `tests/test_evaluator_pin.py` fails if the two
disagree, and Cloud Build checks out the commit from the benchmark manifest.

## Refresh history

- **2026-11-24:** `f5a7dea` (`merge_pr_61493`, 2026-07-23) →
  `ead4350d` (`merge_pr_65618`, 2026-11-23). Corpus version 0.3.0 → 0.4.0.
  - `idlharness.md` documents `set_test_filter()`: added IDLHARNESS-005.
  - `testdriver.md` documents new commands (`create_window`, `navigate`,
    `refresh` and four `bidi.emulation` overrides). The rules don't
    enumerate commands, so no rule changed; TESTDRIVER-004's line anchor
    moved.
  - `print-reftests.md`: typo fixes only.
  - Upstream lint added `TESTHARNESSREPORT-WITHOUT-TESTHARNESS` and
    `TESTDRIVER-VENDOR-WITHOUT-TESTDRIVER` (now listed as covering
    TESTHARNESS-001 and TESTDRIVER-002), extended
    `DUPLICATE-CASE-INSENSITIVE-PATH` to directories, and replaced the
    `WEB_FEATURES.yml` checks for RFC 237. None overlap `lint_ext.py`.
  - New docs not added as sources, because they don't govern test file
    content: `ai-policy.md` (LLM disclosure in commits and PR text),
    `out-of-band-metadata.md` changes (the RFC 237 `WEB_FEATURES.yml`
    format), and the `aamtest.md` events section.

## Upstream source documents

Rules currently draw from:

- `wpt/docs/writing-tests/general-guidelines.md`
- `wpt/docs/writing-tests/testharness.md`
- `wpt/docs/writing-tests/file-names.md`
- `wpt/docs/writing-tests/reftests.md`
- `wpt/docs/writing-tests/print-reftests.md`
- `wpt/docs/writing-tests/idlharness.md`
- `wpt/docs/writing-tests/manual.md`
- `wpt/docs/writing-tests/crashtest.md`
- `wpt/docs/writing-tests/visual.md`
- `wpt/docs/writing-tests/wdspec.md`
- `wpt/docs/writing-tests/testdriver.md`
- `wpt/docs/writing-tests/assumptions.md`
- `wpt/docs/writing-tests/rendering.md`
- `wpt/docs/writing-tests/server-features.md`
- `wpt/docs/writing-tests/css-metadata.md`
- `wpt/docs/reviewing-tests/checklist.md`

## Deterministic rules and the linter extension

Each rule carries a `layer` field: `deterministic` (mechanically checkable
from a file's bytes, structure, or path) or `semantic` (requires rendering
or judgment). The deterministic rules split further by whether upstream
`wpt lint` already enforces them — those it covers are skipped by the
evaluator; those it does not are the "gap set" implemented in
`wptgen/lint_ext.py`, each check named with its `rules.yaml` id.

The full mapping (covered / gap / reclassified-to-semantic) is documented
in [`references/linter-gap-analysis.md`](references/linter-gap-analysis.md).
It was determined against upstream's rule inventory at
`wpt/tools/lint/rules.py` and `lint.py` as of the pinned commit above. Both
the corpus and the upstream linter can change independently, so the mapping
is not permanent — it must be re-checked on every refresh.

## Refresh procedure

When upgrading to a newer upstream commit:

1. Fetch the new commit (`git -C ../wpt fetch upstream <sha>`) and diff the
   source documents and upstream lint against the current pin:
   `git -C ../wpt diff <old>..<new> -- docs/writing-tests docs/reviewing-tests tools/lint/rules.py tools/lint/lint.py`.
   Also check for new documents under `docs/writing-tests/`.
2. Re-read each changed source document; check for added, removed, or
   reworded normative statements. Check every rule's `source` line anchor
   still points at the same text.
3. Update `references/rules.yaml`: add new rules, deprecate removed ones, and
   reword existing ones whose source language has changed. Bump the `source`
   line numbers if they have shifted. Set each rule's `layer` correctly
   (`deterministic` only if decidable from bytes/structure/path — not if it
   needs rendering or judgment).
4. **Re-run the linter gap analysis.** Compare the current
   `layer: deterministic` rules against upstream's rule inventory
   (`wpt/tools/lint/rules.py` and `lint.py`). If upstream added a check that
   now covers a gap rule, remove the corresponding check from
   `wptgen/lint_ext.py` (and its test) to avoid double-flagging. If a newly
   added or newly-`deterministic` corpus rule is NOT covered upstream, add it
   to the gap set for implementation in `lint_ext.py`.
5. Update the tables in
   [`references/linter-gap-analysis.md`](references/linter-gap-analysis.md)
   if the mapping changed.
6. Update `wpt_upstream_commit` (and its comment) in `references/rules.yaml`
   and `benchmarks/manifest.yaml`, and bump the rules `version`.
7. Re-review the benchmark seed labels against the changed rules, check the
   corpus files still exist at the new commit, then set `rules_version` in
   `benchmarks/manifest.yaml` to the new rules version.
8. Run the evaluator benchmark at the release tier to record a new baseline.
9. Add an entry to "Refresh history" above.
