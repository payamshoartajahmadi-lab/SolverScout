"""Frozen research manifests and the headless baseline comparison runner."""

from __future__ import annotations

import hashlib
import statistics
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from p_mc_optimizer.domain.models import (
    LLM_POLICIES, CreateJob, PolicyId, ProblemInput, ProfileReference, strict_json_loads,
)
from p_mc_optimizer.problems.registry import problem_hashes
from p_mc_optimizer.reporting import json_bytes


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Instance(ManifestModel):
    instance_id: str
    problem: str
    problem_math_hash: str
    split: Literal["train", "development", "test"]
    dimensions: int = Field(ge=2, le=30)
    generator_seed: int | None


class Manifest(ManifestModel):
    schema_version: Literal["1.0"]
    suite_id: str
    generator_version: str
    reference_file: str
    provider_profile_revision: ProfileReference | None
    instances: list[Instance] = Field(min_length=1)
    policy_ids: list[PolicyId] = Field(min_length=1)
    fixed_algorithm_id: str
    master_seeds: list[int] = Field(min_length=1)
    budget: int
    max_compute_seconds: int

    @model_validator(mode="after")
    def check_profile(self):
        uses_llm = any(policy in LLM_POLICIES for policy in self.policy_ids)
        if uses_llm != (self.provider_profile_revision is not None):
            raise ValueError("پروفایل تثبیت‌شده فقط برای مجموعهٔ دارای سیاست مدل زبانی لازم است.")
        return self


def _within(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("فایل مسئله یا مرجع باید در پوشهٔ مجموعه باشد.")
    return path


def load_manifest(path: Path) -> tuple[Manifest, str]:
    content = path.read_bytes()
    manifest = Manifest.model_validate(strict_json_loads(content, limit=1024 * 1024))
    return manifest, hashlib.sha256(content).hexdigest()


def plan_jobs(manifest: Manifest, base: Path) -> list[tuple[Instance, CreateJob]]:
    """Validate frozen instances and build job requests without reading references."""
    planned = []
    for instance in manifest.instances:
        problem = ProblemInput.model_validate(strict_json_loads(_within(base, instance.problem).read_bytes(),
                                                                limit=256 * 1024))
        digest, _ = problem_hashes(problem)
        if digest != instance.problem_math_hash or len(problem.variables) != instance.dimensions:
            raise ValueError(f"مدل تثبیت‌شده با manifest مطابقت ندارد · {instance.instance_id}")
        for policy in manifest.policy_ids:
            for seed in manifest.master_seeds:
                policy_input = {
                    "policy_version": "1.0", "portfolio_id": "continuous_v1", "selection_policy": policy,
                    "fixed_algorithm_id": manifest.fixed_algorithm_id if policy == "fixed_solver" else None,
                    "max_objective_evaluations": manifest.budget, "max_compute_seconds": manifest.max_compute_seconds,
                    "master_seed": seed, "explain_via_llm": False,
                }
                profile = manifest.provider_profile_revision if policy in LLM_POLICIES else None
                job = CreateJob.model_validate({
                    "input": {"kind": "structured_problem", "problem": problem},
                    "policy": policy_input,
                    "llm_profile": profile,
                })
                planned.append((instance, job))
    return planned


def run_benchmark(repo, path: Path, solve):
    manifest, manifest_hash = load_manifest(path)
    if manifest.provider_profile_revision is not None:
        raise ValueError("اجرای CLI فقط سیاست‌های پایه را می‌پذیرد. مجموعهٔ دارای مدل را در رابط محلی آغاز کنید.")
    base = path.resolve().parent
    refs = strict_json_loads(_within(base, manifest.reference_file).read_bytes())
    planned = plan_jobs(manifest, base)
    runs = []
    for instance, job in planned:
        problem = job.input.problem.model_dump(mode="json")
        policy = job.policy.model_dump(mode="json")
        result = solve(repo, problem, policy)["report"]
        raw = result["objective_raw"]
        reference = refs.get(instance.instance_id, {}).get("f_star")
        # Analytical metadata enters only here, after the Job is terminal.
        gap = abs(raw - reference) if raw is not None and reference is not None else None
        runs.append({
            "instance_id": instance.instance_id, "split": instance.split, "problem_math_hash": instance.problem_math_hash,
            "policy": policy["selection_policy"], "master_seed": policy["master_seed"], "job_id": result["job_id"],
            "job_status": result["job_status"], "solution_status": result["solution_status"],
            "selected_algorithm_id": result["selected_algorithm_id"], "objective_raw": raw, "f_star": reference,
            "absolute_error": gap, "started_evaluations": result["budget"]["known_started"],
            "elapsed_compute_seconds": result["timing"]["elapsed_compute_seconds"],
            "llm_calls": len(result["llm_calls"]), "elapsed_llm_seconds": result["timing"]["elapsed_llm_seconds"],
        })
    summary = []
    for instance in manifest.instances:
        for policy in manifest.policy_ids:
            group = [x for x in runs if x["instance_id"] == instance.instance_id and x["policy"] == policy]
            scores = [x["absolute_error"] for x in group if x["absolute_error"] is not None]
            quartiles = statistics.quantiles(scores, n=4, method="inclusive") if len(scores) > 1 else None
            validated = sum(x["solution_status"] == "valid_best_found" for x in group)
            summary.append({"instance_id": instance.instance_id, "policy": policy, "runs": len(group),
                            "validated": validated, "validation_rate": validated / len(group),
                            "failed_runs": len(group) - validated,
                            "median_absolute_error": statistics.median(scores) if scores else None,
                            "iqr_absolute_error": quartiles[2] - quartiles[0] if quartiles else None,
                            "total_started_evaluations": sum(x["started_evaluations"] for x in group),
                            "total_compute_seconds": sum(x["elapsed_compute_seconds"] for x in group),
                            "total_llm_calls": sum(x["llm_calls"] for x in group),
                            "total_llm_seconds": sum(x["elapsed_llm_seconds"] for x in group)})
    output = {"suite_id": manifest.suite_id, "manifest_sha256": manifest_hash,
              "run_count": len(runs), "failed_runs": sum(x["failed_runs"] for x in summary),
              "summary": summary, "runs": runs}
    archive = repo.data_dir / "benchmarks"
    archive.mkdir(exist_ok=True)
    name = output["manifest_sha256"] + "-" + runs[0]["job_id"]
    partial = archive / (name + ".part")
    partial.write_bytes(json_bytes(output))
    final = archive / (name + ".json")
    partial.replace(final)
    return {**output, "benchmark_artifact": str(final)}
