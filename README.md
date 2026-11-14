# SolverScout

Local, budgeted experiments for choosing and running numerical optimization algorithms, with optional language-model assistance through an API.

**راهنمای فارسی:** [نصب، تنظیم API و اجرای برنامه](README_FA.md)

## University delivery candidate

Version 2.1.0rc2 prepares six installed MEALPY implementations — ABC, PSO, CMA-ES, DE, GWO and WOA — for bounded, deterministic, single-objective continuous problems. Environment-specific qualification precedes use. The API model proposes three candidates from the qualified catalog; measured pilot runs determine the winner. Reports preserve numerical verification, evidence, evaluation budgets, seeds, implementation versions and the origin of analytical text.

The university acceptance campaign on rc1 closed `NOT_READY`: U1 passed, while U2's mathematical problem was incorrectly rejected by the formulator. The rc2 prompt correction is an offline repair; its fresh live acceptance remains untested. See the delivery report rather than treating installation success as completed university acceptance.

Follow the [university installation and demo guide](DELIVERY_GUIDE_FA.md). The delivery acceptance report records whether this candidate passed all 15 university criteria; older campaign failures remain historical facts.

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

The [version 2 development specification](docs/P_MC_Web_Research_Optimization_Development_Spec_v2.0.md) describes live research in user-selected web sources, evidence-backed suggestions, and an extensible catalog of qualified solver implementations. **The development preview on `dev` implements the versioned catalog and the bounded Tavily workflow. See the [v2 setup and migration guide](docs/UPGRADE_V2_FA.md) and [verification results](TEST_RESULTS_V2.md) for the tested scope.**

## Verification and documentation

- [Quick start](QUICKSTART_FA.md)
- [Recorded test results and limitations](TEST_RESULTS.md)
- [Implementation status](IMPLEMENTATION_STATUS.md)
- [Release notes](RELEASE_NOTES.md)

SolverScout was previously named P_MC. No API keys or user run databases are included in this repository.
