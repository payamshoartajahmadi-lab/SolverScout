# SolverScout

Local, budgeted experiments for choosing and running numerical optimization algorithms, with optional language-model assistance through an API.

**راهنمای فارسی:** [نصب، تنظیم API و اجرای برنامه](README_FA.md)

## First university Windows delivery

**SolverScout — First University Windows Delivery** is a pre-release tagged `v2.1.0-rc.3`; the application version remains `2.1.0rc3`. Clean Windows installation, service health, UI, stop and restart passed with a separate environment and data directory. Run **only one instance at a time**, including when ports or data directories differ. Clean Mac installation and its complete acceptance path (AC14) remain unaccepted; the overall acceptance matrix remains `NOT_READY`.

Version 2.1.0rc3 prepares six installed MEALPY implementations — ABC, PSO, CMA-ES, DE, GWO and WOA — for bounded, deterministic, single-objective continuous problems with 2–30 real variables and no general constraints. Environment-specific qualification precedes use. The API model proposes three candidates from the qualified catalog; measured pilot runs determine the winner. Reports preserve numerical verification, evidence, evaluation budgets, seeds, implementation versions and the origin of analytical text.

Use the [Windows installation guide](docs/WINDOWS_RC3_INSTALL_FA.md), [university preparation guide](DELIVERY_GUIDE_FA.md) and [release notes](docs/delivery/windows-rc3/RELEASE_BODY_FA.md). The release retains the tested ZIP with manifest commit `32e222e21550f0b881c7e54f180043c7589c13bc`; its tag also includes delivery records and merge history with the same executable code.

Recorded rc3 verification is 1,023 offline tests passed and one deselected. U1 report regeneration passed using stored computations; it is not a new complete solve. Historical Windows rc2 results remain U2 passed, U1 numerically valid with a mixed report, and `NOT_READY` after 17 model submissions. The separate 965-pass offline rc2 baseline and unrun live rc2 Mac acceptance remain unchanged; see [verification results](TEST_RESULTS_V21.md).

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
