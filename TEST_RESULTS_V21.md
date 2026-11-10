# SolverScout 2.1 verification

## L0 baseline

Baseline commit `fb483cf`. Tests ran in a separate checkout on macOS arm64 with Python 3.12.16. The first run recorded 163 passed, 38 failed and 1 live test deselected. Streamlit's AppTest had replaced the process entry module, which prevented later spawned numeric workers from starting. Restoring that module at test teardown resolved the interference. The isolated rerun recorded **201 passed and 1 deselected in 109.93 seconds**.

The version dispatch inventory covers job admission in `application/jobs.py`, research routes and frozen inputs in `api/app.py` and `api/research_v2.py`, workflow selection in `workflow/worker.py`, LLM role schemas and call budgets in `llm/`, report recovery in `persistence/repository.py`, exports in `reporting/` and job creation and display in `ui/`.

The labeled corpus in `tests/fixtures/library_v21_corpus.json` freezes three qualified method families, one fictional research-only method and an unrelated document. It includes text hashes and reference offsets. It represents controlled acceptance data.

## L1 contracts and migration

The migration, contract and prior research suites passed with **47 tests in 0.29 seconds**. The cases cover schema 2 to 3, a paired database and artifact backup, rejection with unfinished work, typed retrieval paths and existing version 2 contracts. Migration tests used temporary databases.

## L2 library storage

Three storage tests passed in 0.17 seconds. They cover version reuse and observation changes, rollback of staged metadata and index data, quota limits and idempotent import of verified legacy artifacts with their original validation times.

## L3 direct source refresh

The direct acquisition and existing research suites passed with **62 tests**. The six new sync cases cover seed, sitemap and link discovery, request charging, conditional refresh, removal, permissions, XML safety, overlapping runs, cancellation and interrupted reservations.

## L4 local evidence retrieval

Five retrieval tests passed in 0.20 seconds. They check exact passage offsets, targeted variants, snapshot replay after refresh and source revocation, stale policy, observation scoping for web results, safe FTS queries and corrupt artifacts.

## L5 research orchestration

A targeted run of the optional web path, contracts, original research and model gateway passed **54 tests in 2.23 seconds**. The web fixture exercises HTTP 200 and 304, request accounting and exclusion of unrelated library observations. The version 2.1 planner uses qualified catalog cards and the advisor validates evidence references and variant names. A separate combined regression run of retrieval and UI integration passed 60 tests in 2.34 seconds.

## L6 library interface

The focused Streamlit AppTest and retrieval suite passed with **8 tests in 1.05 seconds**. It used the local API with a fixture transport to register a source, start a sync, inspect the completed run and verify stored text and its hash. It also exercised the version 2.1 local wizard and persistence of the optional provider and its secure credential.

## L7 acceptance and live runs

The final verification used an isolated macOS arm64 instance on Python 3.12.16. The regular version 2 preview still had one job awaiting input. It remained on schema 2. A separate schema 2 instance produced a real verified 1,002-evaluation report. Migration to schema 3 preserved the report hash and passed SQLite integrity and foreign-key checks. The paired backup restored to the original runtime with all three report artifact hashes intact.

| Live operation | Result | Recorded evidence |
| --- | --- | --- |
| Direct acquisition with Tavily disabled and no Tavily credential | PASS | Sync `497e8430-576c-48f1-892c-8a37220f374c` fetched the DE, GWO and WOA MEALPY documentation. Four HTTP requests, 588,786 expanded bytes, three new documents and 2.295 seconds active. A local search returned all three. |
| Local library solve with real model API | PASS | Job `cf1c8a4a-7542-476a-91ed-dc7dfaa51661` used three documents and six passages, with zero research HTTP requests. It ran 5,502 FE, selected OriginalWOA and verified objective `1.9365005055017865e-05`. |
| Direct refresh followed by a solve with real model API | PASS | Job `6f433ef8-9195-4b79-b275-955d433fb461` revalidated all three versions in three conditional requests with zero response body bytes. It selected OriginalWOA and verified the same objective in 5,502 FE. The sync ledger and local retrieval ledger are separate. |
| Optional Tavily connection and web solve | PASS | Connection test completed in 1.928 seconds. Job `c152214d-4169-417e-b08e-1c5add95bdf3` made two Tavily searches and six research HTTP requests. Four documents and eight passages were captured from that run. It completed and verified the same objective in 5,502 FE. Tavily was disabled again after the test. |
| Rebuild library search index | PASS | The stopped isolated instance rebuilt three verified versions and skipped zero. The subsequent direct and web runs succeeded. |
| Direct network access outside the active tunnel | BLOCKED | Routes to both live endpoints used `utun4`. An HTTPS probe bound to `en0` for the MEALPY documentation timed out after six seconds. Live solves demonstrate independence from Tavily in the local and direct modes. No VPN-free live solve is claimed. |
| Explanation by the live model | FAIL | Numeric verification passed on both library runs. The explainer produced valid JSON but semantic validation rejected some Persian limitations that explicitly denied guarantees. The program displayed its template. A regression fix accepts the first observed wording while retaining the positive-certainty checks. Other formulations still triggered the fallback in the second live run. |

Playwright Chromium verified the source library, exact document text and hash, localized validation date and the local wizard at 1440 × 900 and 390 × 844. The wizard showed the web-engine requirement when Tavily was disabled. No app exception overlay, relevant console error or horizontal overflow appeared. The browser smoke test did not submit jobs. Job submission and numerical execution were exercised through the authenticated local API.

The loopback acceptance test uses the public sync route and the normal API lifespan worker with controlled source responses. It checks exact corpus hashes, three cited qualified variants, a research-only discovery, an immutable historical result, direct refresh before preflight, a deliberate stop on insufficient evidence, a 5,502-FE fallback and secret-free exports. The numeric workers in that test are real. The model and source responses are fixtures.

| Criterion | Verification | Evidence |
| --- | --- | --- |
| LIB-01 | PASS | Fresh isolated database and real MEALPY direct sync with no Tavily key. |
| LIB-02 | PASS | Loopback local and direct paths with a disabled engine and an untested fixture key. The research adapter fails the test if called. |
| LIB-03 | PASS | Bounded seed and internal-link fixture with depth and skip counts. |
| LIB-04 | PASS | Registered compressed sitemap outside content path, rejected out-of-scope links. |
| LIB-05 | PASS | Hostile XML, private redirect and mixed/private IP tests. |
| LIB-06 | PASS | Version and observation tests plus real conditional revalidation. |
| LIB-07 | PASS | Timeout accounting, 403, 429 and invalid PDF cases preserve usable content and its validation time. |
| LIB-08 | PASS | 404/410 observations preserve prior versions and record remote unavailability. Missing links do not trigger deletion. |
| LIB-09 | PASS | Concurrent overlapping submissions, idempotency and owner-specific cancellation. |
| LIB-10 | PASS | Expired lease and reserved-request recovery fixtures plus failed publication rollback. |
| LIB-11 | PASS | Cancellation while receiving content leaves no published staging. |
| LIB-12 | PASS | Research transports blocked in the controlled test. The live local run charged zero research HTTP requests. |
| LIB-13 | PASS | Readiness, stale opt-in and disabled-source retrieval cases. |
| LIB-14 | PASS | Scope changes after freezing evidence preserve the existing snapshot. Revocation during refresh prevents new publication. |
| LIB-15 | PASS | FTS operators passed as literal normalized terms. |
| LIB-16 | PASS | Controlled corpus and actual MEALPY documents both yielded three cited choices and verified numeric solves. |
| LIB-17 | PASS | Irrelevant corpus stops before pilot in the controlled test. Explicit fallback completes separately. |
| LIB-18 | PASS | Fictional Orbit Lattice Search recorded as research-only from fixture text. |
| LIB-19 | PASS | Catalog aliases, exact variant matching and stable evidence offsets tested. |
| LIB-20 | PASS | Guarded migration, paired restore and repeat legacy import without duplicate versions. |
| LIB-21 | PASS | Restarted browser preview reads persisted documents and settings. Credential persistence also exercised by AppTest. |
| LIB-22 | PASS | Existing version 1 and 2 workflows and new 2.1 admission, worker and report paths exercised by the suite. |
| LIB-23 | BLOCKED | The tested machine used a VPN tunnel. The physical-interface HTTPS probe timed out. |
| LIB-24 | PASS | Live optional web solve, plus 200/304 fixture verifying exact same-job observation scoping. |
| LIB-25 | PASS | Truncated document retains its truncation flag and the missing tail cannot be retrieved. |
| LIB-26 | PASS | Export checks omit credentials and internal token. Quota rollback and orphan cleanup preserve verified versions. |

Windows, Linux and macOS Intel installation and browser tests were not run. Live model response and network availability depend on the configured provider and connection.

### Final regression and package checks

`.venv/bin/python -m pytest -q --tb=short` completed with **233 passed and 1 deselected in 148.44 seconds**. The earlier full run before the four extra retention and truncation cases recorded 229 passed and 1 deselected. The provider smoke test deselected by pytest is separate from the live API operations above.

Doctor reported project version `2.1.0.dev0`, schema 3 and a working FTS5 index. The source ZIP check verified the archive checksum, all 242 manifest file sizes and SHA-256 hashes, the new prompts, this verification record and the UI font. The archive contained no local databases, logs or environment files.

### Targeted evidence coverage rerun

The quadratic job `a03c2372-45b7-4dcd-ace4-a8348442bf6d` used both bounds `[-5, 5]` and an empty parameter object. Its six documents yielded twelve passages, with no DE citation. Generic document ranking had excluded the dedicated DE page, and a package page containing DE was used only for general discovery. The archived-data replay retrieved the exact OriginalDE, OriginalGWO and OriginalWOA documentation after the change. The earlier stored snapshot was read only.

Retrieval now reserves passages for up to three distinct targeted implementations before filling the remaining six-document and twelve-passage budget. It searches document bodies for exact variants first and considers family aliases afterward. The excerpts preserve source text and offsets. A regression fixture crowds the ranking with twenty-four general documents whose titles contain the target names.

The first UI rerun retrieved all three exact passages. The advisor cited them and used `config_id` as `card_ref`. The prompt and repair diagnostics now name the required fields and the existing reference check enforces their mapping. Another run retrieved all three passages and reached the advisor's sixty-second provider deadline. The report now retains the provider's timeout message.

Job `f2a46f81-44f9-475c-9a28-81b632c3fdab` was submitted through the browser wizard using the frozen problem, direct refresh, the original stop policy, 20,000 evaluations and seed 1405. The advisor cited the three implementations from six documents and ten passages. Nine pilot trials and the main run completed. OriginalDE returned `[2.0, -1.0]` with objective `0.0`. Both verification evaluations returned `0.0`. The report displayed its template explanation after two explainer validation failures.

`.venv/bin/python -m pytest -q tests/test_library_v21_workflow.py tests/test_library_v21_retrieval.py tests/test_library_v21_web.py tests/test_llm_gateway.py --tb=short` completed with **42 passed in 38.01 seconds**. Playwright Chromium submitted and displayed the completed result at 1440 × 980 with no page errors.
