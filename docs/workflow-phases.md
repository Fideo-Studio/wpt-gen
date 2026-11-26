# WPT-Gen Workflow Phases

The `wpt-gen generate` command orchestrates a complex, multi-phase agentic workflow to create Web Platform Tests. This document details the internal logic, responsibilities, inputs, outputs, and LLM integrations for each individual phase located in the `wptgen/phases/` module.

## Phase 1: Context Assembly (`context_assembly.py`)

*   **Responsibility:** Aggregates the "Source of Truth" from external documentation (W3C Specs, MDN) and identifies existing test coverage in the local WPT repository.
*   **Inputs:** `web_feature_id` (string), `Config` object, `UIProvider`.
*   **Outputs:** A hydrated `WorkflowContext` object containing the web-features metadata, scraped specification context, and local WPT tree paths.
*   **Spec sections:** A spec URL with a `#fragment` is cut down to that section: the section element or heading the anchor is in, up to the next heading at the same level. If the anchor isn't found, the whole document is used, with a warning.
*   **LLM Integration:** None. This phase is entirely focused on gathering local and remote factual data deterministically to feed the subsequent phases.

## Phase 2: Requirements Extraction (`requirements_extraction.py`)

*   **Responsibility:** Synthesizes specification text into structured, granular technical requirements. It supports parallel, categorized, and iterative extraction modes for complex or large specs.
*   **Inputs:** `WorkflowContext` (containing the spec context), `Config`, `LLMClient`, `UIProvider`, `jinja_env`, and an optional cache directory `Path`.
*   **Outputs:** A string of XML containing the extracted requirements, which is then attached to `WorkflowContext.requirements_xml`.
*   **LLM Integration:** Uses the LLM extensively to process the raw HTML/text of the spec and transform normative statements into isolated, testable requirements. It utilizes three different versions of prompts depending on configuration flags:
    *   **Categorized (Default):** Extracts requirements mapped to logical categories (e.g., API behavior, security, IDL).
        *   **System Prompt:** `requirements_extraction_categorized_system.jinja`
        *   **User Prompt:** `requirements_extraction_categorized.jinja`
    *   **Iterative (via `--detailed-requirements` flag):** Iterates through the specification chunk by chunk for very large and complex specs.
        *   **System Prompt:** `requirements_extraction_iterative_system.jinja`
        *   **User Prompt:** `requirements_extraction_iterative.jinja`
    *   **Single Prompt (via `--single-prompt-requirements` flag):** A simpler approach attempting extraction in a single pass without categorization.
        *   **System Prompt:** `requirements_extraction_system.jinja`
        *   **User Prompt:** `requirements_extraction.jinja`
    *   A request that still fails after the LLM client's retries fails the phase; a failed category is never treated as one with no requirements.

## Phase 3: Coverage Audit (`coverage_audit.py`)

*   **Responsibility:** Performs a delta analysis by comparing the synthesized requirements against the local test suite. This phase identifies gaps in coverage and produces high-level test suggestions.
*   **Inputs:** `WorkflowContext` (with requirements and local test tree), `Config`, `LLMClient`, `UIProvider`, `jinja_env`.
*   **Outputs:** A string of XML representing the audit response (including missing test suggestions), saved to `WorkflowContext.audit_response`.
*   **LLM Integration:** Uses the LLM to cross-reference extracted requirements with existing test files to intelligently determine what new tests are needed. It automatically partitions requirements into chunks (e.g., `max_threshold=40`) to remain within context limits. The phase fails if any partition's request fails, or if the merged worksheet doesn't have exactly one row per requirement.
    *   **System Prompt:** `coverage_audit_system.jinja`
    *   **User Prompt:** `coverage_audit.jinja`

## Phase 4: Test Generation (`generation.py`)

*   **Responsibility:** Translates user-selected test suggestions into functional WPT-compliant code (e.g., JavaScript tests, Reftests, or Crashtests) using specific style guide instructions.
*   **Inputs:** `WorkflowContext` (with the audit response/test suggestions), `Config`, `LLMClient`, `UIProvider`, `jinja_env`.
*   **Outputs:** A list of generated test files, each represented as a tuple of `(Path, string content, suggestion_xml)`. Saves the initial versions of these files to disk.
*   **Run records:** Every run the agent starts through `run_wpt_test` is recorded. When an agent finishes, each test it wrote gets a final local run: the agent's last run is reused if no file changed after it; otherwise wpt-gen runs the test once more. The record (browser and version, overall status, each subtest's result, the agent's attempts, and any failures the agent reports as browser bugs) is saved under `run` in `generated_tests.json`. A test whose final run has failed assertions or a harness error is listed under "Needs review" in the summary, as is a suggestion whose agent never reported its files. With `--fail-on-flagged`, either makes the run exit with an error. Manual tests, and every test under `--skip-run`, are recorded as not run.
*   **Evaluation:** `scripts/evaluate_generated_tests.py STATE_DIR` runs `wpt-gen evaluate` on each generated test, with its suggestion's spec URLs.
*   **LLM Integration:** The LLM receives the test suggestion, WPT authoring guidelines, and relevant spec context to generate valid, idiomatic code for the new test files. Each suggestion also lists up to three existing tests the audit cited as covering related requirements (those in the same category first), and the directory that holds most of the audited tests, so the agent can start from them instead of searching. The request also includes the spec text (unless it's over 60,000 characters), the files in that directory and `wpt_style_guide.md`, ahead of the suggestion, so agents in one run share a prompt prefix. Each agent's time, model and tool calls, tokens (including cached ones) and test runs are saved to `agent_usage.json` in the state directory.
    *   **System Prompt:** `test_generation_system.jinja`
    *   **User Prompt:** `test_generation.jinja`


## Phase 5: Web Features Mapping (`mapping.py`)

*   **Responsibility:** Maps each generated test to the web features it covers, in its directory's `WEB_FEATURES.yml` (RFC 237 `rules:` format), and checks the result with WPT's own tooling.
*   **Inputs:** `WorkflowContext` (generated tests, the feature IDs the generator declared for each test, and the manifest resolved at the start of the run), `Config`, `UIProvider`, the state directory.
*   **Steps:**
    1. WPT's `SourceFile` decides which generated files are tests; references and helpers are never mapped.
    2. Each test is mapped to the features the generator declared for it (by default, the target feature only), plus any it was already mapped to.
    3. The planned mappings are listed and confirmed before anything is written. Mappings that need a judgment call are flagged for review: extra features the generator declared (with its reason), existing mappings that are kept, and features the directory's rules add. `--yes-tests` skips the prompt. Feature IDs that aren't in web-features stop the run.
    4. `wptgen/metadata.py` writes the rules as text, keeping comments and rule order.
    5. If any file changed, WPT's web-features manifest is rebuilt from the working tree. The phase fails if a test isn't mapped as planned, or if another test under an edited directory lost a mapping.
*   **Outputs:** `web_features_mapping.json` and `web_features_manifest.after.json` (not cached) in the state directory.
*   **LLM Integration:** None. The generator declares features through `report_generation_complete`.
*   **Opt out:** `--no-wf-yml-update`. Not run for ChromeStatus IDs.
