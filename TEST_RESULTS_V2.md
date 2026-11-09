# SolverScout v2 verification

## P0 baseline

Run on 2026-11-09 using macOS 27.0.1 arm64, Python 3.12.16, SQLite 3.53.1,
MEALPY 3.0.3, NumPy 1.26.0, Pydantic 2.14.3, HTTPX 0.28.1,
FastAPI 0.162.2, Streamlit 1.67.0 and pytest 9.2.0.

| Test | Type | Command | Result | Evidence |
| --- | --- | --- | --- | --- |
| P0 v1 regression | local, fixture and numeric | `.venv/bin/python -m pytest -q` | PASS | 121 passed, 1 deselected, 48.32 s |
| P0 live v1 provider | live | Default pytest marker excludes it | NOT_TESTED | No live request in this run |

Baseline Git revision `58e0bc416db74a1aaa9783da442d8fceca775dab` contains
the same bytes as the local source across all 153 published files.
The baseline commit restores the launcher executable bits and the five
internal prompt symlinks lost in the GitHub web upload. The checks ran
against temporary test data. Existing runtime data was not migrated.

## Development preview

The checks below ran on 2026-11-09 on the P0 macOS arm64 environment.
The new parser versions are pypdf 7.0.0 and Protego 0.7.0. The same
Python executable and locked dependency set were used for numeric and
HTTP fixtures. Test data and reports went to isolated directories.

| Scenario | Type | Command or action | Result and evidence |
| --- | --- | --- | --- |
| Strict contracts and source policy | fixture | `.venv/bin/python -m pytest -q tests/test_research_contracts.py` | PASS, 39 cases including both specification examples, strict JSON, URL scope and FE boundaries |
| Search adapter, cache, robots and extraction | fixture | `.venv/bin/python -m pytest -q tests/test_v2_research.py` | PASS, 10 cases covering scoped search, blocked redirects, IP pinning/SNI, compressed size, PDF, cache, idempotency and key guards |
| Public document transport | mixed | Inject one search result for the official MEALPY documentation, then use the real pinned HTTPS transport and extractor | PASS, robots and one HTML page actually fetched, two evidence passages, 1.10 s active research. Search result was synthetic, with no Tavily or model API call |
| Discovery, shortlist and numerical qualification | fixture + numeric | `.venv/bin/python -m pytest -q -s tests/test_catalog_v2.py tests/test_numeric.py` | PASS, 76 cases. MEALPY 3.0.3 exposed 234 classes. Six configurations qualified, including new GWO and WOA. Per configuration, five successful 500-FE trials and one deliberate setup fault charged at 1 FE. 15,006 FE across six configurations |
| Large shortlist | synthetic catalog | CAT-006/007 in `test_catalog_v2.py` | PASS, 10,000 cards reduced to 20, diversity and 9,980 exclusions verified. One measured run took 0.105 s and 5,634,456 peak traced Python bytes |
| Research, catalog comparison, fallback and cache | fixture + numeric | `.venv/bin/python -m pytest -q tests/test_v2_workflow.py` | PASS. Loopback HTTP, model/search fixtures, actual numeric workers. The web and catalog comparison holds problem, three configs, seed and 5,502 FE constant. Fixture recommends the same three implementations. It does not measure the value of web research |
| Citation scoring | labeled synthetic corpus | `.venv/bin/python scripts/evaluate_citations_v2.py --control` and `tests/test_v2_citation_evaluation.py` | PASS, 24 labeled cases, 8 per verdict. Oracle control 100%, zero wrong variant or execution attributions and zero invalid references. Five evaluator tests passed. No independent model prediction scored |
| Browser interactions | actual browser + local numeric | Codex browser, 1280 × 720, loopback preview on port 18501 | PASS, app identity/contents and no app console errors observed. Search key recorded and cleared with fixture string, then removed. Catalog discovery reported 234 classes and real GWO qualification completed at 2,501 of at most 3,000 FE, zero unknown. Other viewport sizes untested |
| Fresh installation | actual local install | `scripts/setup.sh` in clean worktree with Python 3.12 | PASS, dependency install, pip check, doctor, and a real verified v1-policy solve with 1,002 FE. macOS arm64 only |
| V1 migration and restore | actual local SQLite + numeric | Run v1 solve on a clean data dir. Migrate it, open its v1 report from v2, then copy backup and artifacts to a fresh directory and open with v1 | PASS, both runtimes read the v1 report and all restored artifact hashes matched. Automated migration tests also cover pending jobs and charged unknown FE |
| Final full suite | fixture + numeric + integration | `.venv/bin/python -m pytest -q --tb=short` | PASS, 186 passed, 1 live test deselected, 125.60 s |
| Package contents | local source archive | `.venv/bin/python -m p_mc_optimizer package` and ZIP manifest/hash check | PASS, all packaged bytes verified. No user DB, secret file, web cache or logs in archive |
| Live Tavily and live model Job | live | Configure valid credentials in the UI before attempting LIVE2 | BLOCKED, no Tavily credential for the isolated test run. No provider usage or price assumed |
| Windows and Linux installation/browser | actual environment | Run setup, migration and UI smoke on each platform | NOT_TESTED |

The official adapter request fields were checked against the
[Tavily Search API documentation](https://docs.tavily.com/documentation/api-reference/endpoint/search).
The acceptance matrix records remaining gaps individually.
