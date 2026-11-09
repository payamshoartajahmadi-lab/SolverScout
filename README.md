# SolverScout

Local, budgeted experiments for choosing and running numerical optimization algorithms, with optional language-model assistance through an API.

**راهنمای فارسی:** [نصب، تنظیم API و اجرای برنامه](README_FA.md)

## Current release

Version 1 implements four MEALPY solvers — ABC, PSO, DE and CMA-ES — for bounded, deterministic, single-objective continuous problems. Candidate solvers are compared through measured pilot runs. Reports record evaluation budgets, seeds, implementation versions and independently verified results.

The interface and numerical engine run locally. Language-model requests use the provider and API key configured in the application. The internal Python package retains its original `p_mc_optimizer` name.

## Run locally

Use Python 3.12. On macOS or Linux, run from the repository directory:

```sh
chmod +x scripts/*.sh
./scripts/setup.sh
./scripts/start.sh
```

Open http://127.0.0.1:8501 in your browser. For Windows, use the `.bat` scripts described in the [Persian guide](README_FA.md).

## Development plan

The [version 2 development specification](docs/P_MC_Web_Research_Optimization_Development_Spec_v2.0.md) describes live research in user-selected web sources, evidence-backed suggestions, and an extensible catalog of qualified solver implementations. **Version 2 is a specification; its web-search and catalog features have not been implemented yet.**

## Verification and documentation

- [Quick start](QUICKSTART_FA.md)
- [Recorded test results and limitations](TEST_RESULTS.md)
- [Implementation status](IMPLEMENTATION_STATUS.md)
- [Release notes](RELEASE_NOTES.md)

SolverScout was previously named P_MC. No API keys or user run databases are included in this repository.
