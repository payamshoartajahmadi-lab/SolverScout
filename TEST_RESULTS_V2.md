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
