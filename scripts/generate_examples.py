"""Rebuild the versioned example manifests and offline analytical references."""

from pathlib import Path
from random import Random

from p_mc_optimizer.domain.models import ProblemInput
from p_mc_optimizer.problems.registry import problem_hashes
from p_mc_optimizer.reporting import json_bytes

ROOT = Path(__file__).resolve().parents[1] / "examples"
FAMILIES = {"sphere": 5.0, "rosenbrock": 3.0, "rastrigin": 5.12, "ackley": 32.768}


def make_problem(expression, dimensions=2, bound=5.0, sense="minimize", parameters=None):
    return {
        "schema_version": "1.0",
        "variables": [{"name": f"x{i}", "label_fa": None, "type": "real", "lower": -bound,
                       "upper": bound, "unit": None} for i in range(1, dimensions + 1)],
        "objective": {"sense": sense, "unit": None, "source": {"kind": "expression", "expression": expression}},
        "parameters": parameters or {}, "constraints": [], "deterministic": True,
    }


def write(name, payload):
    path = ROOT / name
    path.parent.mkdir(exist_ok=True, parents=True)
    path.write_bytes(json_bytes(payload) + b"\n")


def combine(terms):
    if len(terms) == 1:
        return terms[0]
    mid = len(terms) // 2
    return f"({combine(terms[:mid])}+{combine(terms[mid:])})"


def objective(family, terms, offset=""):
    n = len(terms)
    squares = combine([f"{x}**2" for x in terms])
    if family == "sphere":
        return squares + offset
    if family == "rosenbrock":
        if n == 20:
            # Factor out the shared coefficient to keep the canonical 20-D
            # formula inside the DSL's fixed 512-node limit.
            valley = combine([f"({b}-{a}**2)**2" for a, b in zip(terms, terms[1:])])
            center = combine([f"(1-{a})**2" for a in terms[:-1]])
            return f"100*{valley}+{center}" + offset
        return combine([f"100*({b}-{a}**2)**2+(1-{a})**2" for a, b in zip(terms, terms[1:])]) + offset
    if family == "rastrigin":
        return f"{10*n}+" + combine([f"({x}**2-10*cos(2*pi*{x}))" for x in terms]) + offset
    cosines = combine([f"cos(2*pi*{x})" for x in terms])
    return f"-20*exp(-0.2*sqrt({squares}/{n}))-exp({cosines}/{n})+20+e" + offset


def entry(instance, relative, problem, split, generator_seed):
    digest, _ = problem_hashes(ProblemInput.model_validate(problem))
    return {"instance_id": instance, "problem": relative, "problem_math_hash": digest,
            "split": split, "dimensions": len(problem["variables"]), "generator_seed": generator_seed}


def main():
    references = {"quadratic_2d": {"f_star": 0.0, "x_star": [2.0, -1.0]},
                  "quadratic_2d_max": {"f_star": 7.0, "x_star": [2.0, -1.0]}}
    quad = make_problem("(x1-2)**2+(x2+1)**2")
    write("quadratic_2d.json", quad)
    write("quadratic_2d_max.json", make_problem("7-((x1-2)**2+(x2+1)**2)", sense="maximize"))
    unsupported = make_problem("(x1-2)**2+(x2+1)**2")
    unsupported["variables"][0]["type"] = "integer"
    write("unsupported_integer.json", unsupported)
    smoke, research = [], []
    for family, bound in FAMILIES.items():
        instance = f"{family}_2d"
        problem = make_problem(objective(family, ["x1", "x2"]), bound=bound)
        write(instance + ".json", problem)
        smoke.append(entry(instance, instance + ".json", problem, "development", None))
        base = 1.0 if family == "rosenbrock" else 0.0
        references[instance] = {"f_star": 0.0, "x_star": [base, base]}
        for dim in (5, 10, 20):
            for replicate, split in enumerate(("development", "test", "train")):
                generator_seed = 140500 + 10 * dim + replicate
                rng = Random(f"{family}|{generator_seed}")
                shifts = [round(rng.uniform(-0.75, 0.75), 6) if dim < 20 or i < 3 else 0.0
                          for i in range(dim)]
                offset = round(rng.uniform(-2.0, 2.0), 6)
                relative = f"research/{family}_{dim}d_{replicate}.json"
                instance = Path(relative).stem
                names = [f"(x{i}-shift{i})" if shift else f"x{i}" for i, shift in enumerate(shifts, 1)]
                problem = make_problem(objective(family, names, "+offset"), dim, bound,
                                       parameters={**{f"shift{i}": x for i, x in enumerate(shifts, 1)}, "offset": offset})
                write(relative, problem)
                research.append(entry(instance, relative, problem, split, generator_seed))
                references[instance] = {"f_star": offset, "x_star": [base + x for x in shifts]}
    write("references.json", references)
    defaults = {"schema_version": "1.0", "generator_version": "1.0", "provider_profile_revision": None,
                "reference_file": "references.json", "fixed_algorithm_id": "de.original"}
    write("benchmark_smoke.json", {**defaults, "suite_id": "continuous-v1-smoke", "instances": smoke,
                                  "master_seeds": [1405, 1406], "policy_ids": ["fixed_solver", "pilot_only"],
                                  "budget": 7002, "max_compute_seconds": 300})
    write("benchmark_research.json", {**defaults, "suite_id": "continuous-v1-research",
                                     "instances": research, "master_seeds": list(range(1405, 1425)),
                                     "policy_ids": ["fixed_solver", "pilot_only", "rule_based", "random_solver"],
                                     "budget": 20000, "max_compute_seconds": 300})


if __name__ == "__main__":
    main()
