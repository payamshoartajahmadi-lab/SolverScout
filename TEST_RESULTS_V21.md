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
