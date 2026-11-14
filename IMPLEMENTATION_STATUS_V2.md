# SolverScout v2 implementation status

Release status is development preview `2.0.0.dev0` on `origin/dev`.
Changes use a `codex/` feature branch, a commit on that branch, then a
merge into `dev`. The version 1 branch retains its release history.

| Phase | Delivered work and verification |
| --- | --- |
| P0 | Re-ran v1 regression and recorded the actual baseline |
| P1 | Versioned schemas, bounded source policy and guarded SQLite upgrade |
| P2 | Tavily adapter, write-only key settings, durable call reservations and planner role |
| P3 | Bounded document fetch, robots, extraction, evidence, cache and provenance |
| P4 | MEALPY discovery, qualification, stored configuration snapshots and bounded shortlist |
| P5 | Versioned jobs, advisor, catalog and web policies, actual numeric pilots and partial results |
| P6 | Streamlit settings, qualification and report views with a local browser smoke test |
| P7 | Matched numeric fixture comparison and labeled citation evaluation. Live validation blocked |
| P8 | Mac arm64 installation, migration, restore, doctor and source package checks |

[Verification details and environment](TEST_RESULTS_V2.md)

An acceptance row is NOT_TESTED when a required scenario lacks a direct
check, even if implementation or partial fixture coverage exists.

| ID | Result | Evidence or limit |
| --- | --- | --- |
| VER-001 | PASS | Actual v1 solve, migration and report reopen in v2. |
| VER-002 | NOT_TESTED | V1 policy regression passed and discovery is separate. Combined post-discovery v1 Job was not explicitly checked. |
| VER-003 | PASS | Qualified GWO and WOA accepted by v2. Unknown runtime configuration rejected in catalog tests. |
| VER-004 | PASS | SQLite rollback and consistent backup verified by migration tests. |
| VER-005 | NOT_TESTED | Immutable snapshots and catalog mutation covered. No joint in-flight settings mutation scenario. |
| VER-006 | NOT_TESTED | Version mismatch guard and historical reading checked separately. Explicit old v2 report rerun scenario untested. |
| VER-007 | PASS | Pending v1 job blocked. Charged unknown FE and old report preserved after upgrade. |
| DAT2-001 | PASS | Strict JSON, model fields and foreign evidence rejected in contract tests. |
| DAT2-002 | PASS | Both specification JSON examples validated by runtime DTOs. |
| WEB-001 | PASS | Scoped Tavily request fixture and local filtering. |
| WEB-002 | PASS | URL policy tests cover hostname, subdomain and path segment boundaries. |
| WEB-003 | PASS | Redirect fixture records rejection before fetching forbidden destination. |
| WEB-004 | PASS | Private/special IP tests, mixed DNS result rejection, pinned address and TLS SNI fixture. Public HTTPS smoke passed. |
| WEB-005 | PASS | Document transport fixture confirms no provider credentials. |
| WEB-006 | NOT_TESTED | Attempt cap and gzip bounds tested. Combined 401, timeout, fourth attempt and elapsed cleanup was not run. |
| WEB-007 | NOT_TESTED | 401 and unknown outcomes tested with fixtures. Full 429/timeout and ledger matrix untested. |
| WEB-008 | PASS | Checkpoint and idempotent test replay fixtures, including conflict checks. |
| WEB-009 | NOT_TESTED | Unknown reservation replay blocked in fixtures. Process kill during a web send was not exercised. |
| WEB-010 | NOT_TESTED | HTML/PDF/scanned PDF and gzip bounds tested. Parser timeout not forced in a fixture. |
| WEB-011 | PASS | New Job runs live search again. Conditional fetch fixture distinguishes 304. |
| WEB-012 | PASS | Loopback cache-only workflow succeeds after deletion of search credential. Zero search/fetch calls. |
| WEB-013 | PASS | Planner wire fixture receives lossy profile and rules only. Job input expression stays out of search. |
| WEB-014 | NOT_TESTED | Research cancellation stops new requests in a fixture. Browser cancellation mid-research untested. |
| WEB-015 | NOT_TESTED | Robots 200/404/403/5xx tested with fixtures. Explicit robots timeout untested. |
| WEB-016 | PASS | Scoped normalization and duplicate-ID tests. |
| WEB-017 | PASS | Cache timestamp and revalidation fixtures. |
| WEB-018 | NOT_TESTED | Round-robin and passage ranking implemented. Exact 15-hit scenario not exercised. |
| EVD-001 | PASS | Evidence offsets and content hashes resolve, and same-Job references are enforced. |
| EVD-002 | PASS | Snippet exclusion and content-level validation. |
| EVD-003 | PASS | Nullable publication metadata and distinct fetch timestamps. |
| EVD-004 | NOT_TESTED | External text is data and has no tool route. Independent adversarial model evaluation untested. |
| EVD-005 | PASS | Loopback test persists a known-only finding without promoting it to a Trial. |
| EVD-006 | NOT_TESTED | Exact variant attribution guard and adversarial scorer tests. Independent semantic validation untested. |
| EVD-007 | NOT_TESTED | Bounded repair shared with v1 and role caps dispatched in v2. Full 8/6 exhaustion matrix untested. |
| EVD-008 | PASS | Full fixture workflow using JSON completions without native tools. |
| EVD-009 | NOT_TESTED | Empty or insufficient plan cannot start numeric pilots. Semantic relevance of evidence awaits model evaluation. |
| EVD-010 | NOT_TESTED | 24-case labeled corpus and oracle control passed. Independent live model scores and human review are pending. |
| CAT-001 | PASS | Actual discovery from MEALPY 3.0.3, no solver started by discovery. |
| CAT-002 | PASS | Opaque constructor fixture. |
| CAT-003 | PASS | Version/config/hash invalidation and preserved snapshots. |
| CAT-004 | PASS | Qualification gate and filtered shortlist. |
| CAT-005 | PASS | GWO and WOA qualified by actual isolated numeric runs. |
| CAT-006 | PASS | 10,000-card test. 20 retained, 9,980 exclusions. See measured time and memory. |
| CAT-007 | PASS | FTS shortlist family diversity test. |
| CAT-008 | PASS | Bounded batch and cancellation tests. Separate FE ledger visible in the browser. |
| NUM-001 | PASS | Real 500-FE trials include setup. Loopback fixture verifies all nine pilot trials. |
| NUM-002 | PASS | Strict policy test, including 14,998 main FE at 20,000 and rejecting total below 5,502. |
| NUM-003 | PASS | Shared pilot-ranking regression verifies an incomplete or failed set is ineligible. |
| NUM-004 | PASS | Shared pilot-ranking regression requires two eligible methods. |
| NUM-005 | PASS | Shared ranking regression validates median, normalized linear IQR and ID. |
| NUM-006 | PASS | Independent numeric subprocess qualification compares repeated traces. |
| NUM-007 | PASS | Adapter tests for maximize, bounds, controlled error, cancellation and timeout. |
| NUM-008 | NOT_TESTED | V1/shared archive and origin regressions passed. V2-specific better-pilot-than-main scenario untested. |
| UI2-001 | PASS | Browser fixture key entry, clearing and deletion. Distinct service namespace. |
| UI2-002 | PASS | Settings fixture and version/generation guards. |
| UI2-003 | NOT_TESTED | Missing setup blocks at 0 FE in a fixture. Full visual remediation of each error untested. |
| UI2-004 | PASS | Loopback fixture checks missing initial key and post-start 401 under both policies. |
| UI2-005 | PASS | Generated report HTML contains saved passages and renders without scripts or external assets. |
| UI2-006 | NOT_TESTED | Loopback-only browser served the UI. Full browser network trace during provider calls untested. |
| UI2-007 | NOT_TESTED | HTML escaping checked end to end. Source CSV attack inputs not exhaustively tested. |
| LIVE2-001 | BLOCKED | The isolated live run has no valid Tavily credential. |
| LIVE2-002 | BLOCKED | The isolated live run has no valid Tavily credential. |
| LIVE2-003 | BLOCKED | The isolated live run has no valid Tavily credential. |
| PKG2-001 | PASS | Fresh install, pip check and doctor on macOS arm64. |
| PKG2-002 | PASS | Actual v1 report reopened after migration and after restore in v1. |
| PKG2-003 | PASS | Source ZIP manifest and every content hash verified. User data and runtime files excluded. |
| PKG2-004 | PASS | Fixture, numeric, browser and live status recorded separately. |
