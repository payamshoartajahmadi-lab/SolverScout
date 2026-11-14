"""Bounded, explicitly submitted structured comparisons through the local API."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import streamlit as st

from p_mc_optimizer.benchmarks import Manifest, load_manifest, plan_jobs
from p_mc_optimizer.problems.registry import canonical_json


_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
_BASELINES = {
    "fixed_solver": "خط پایه · روش ثابت DE",
    "random_solver": "خط پایه · روش تصادفی",
    "rule_based": "خط پایه · قاعدهٔ ثابت",
    "pilot_only": "خط پایه · پایلوت چهار روش",
}
_MODEL_POLICIES = {
    "llm_only": "مشاور بدون پایلوت",
    "llm_shortlist_then_pilot": "مشاور و پایلوت",
}


def research(attempt, *, open_execution=None):
    st.header("مقایسهٔ پژوهشی")
    st.caption("یک مسئلهٔ ساخت‌یافته با بودجه و بذر یکسان در دو سیاست اجرا می‌شود. هر اجرا در تاریخچهٔ محلی گزارش مستقل دارد.")
    settings = attempt("GET", "/settings/llm/profile")
    profile = (settings or {}).get("profile")
    ready = bool(
        profile and profile["test_status"] == "passed"
        and (settings.get("credential_status") or {}).get("available")
    )
    if not ready:
        st.info("برای مقایسه با مشاور، ابتدا کلید و مدل را ثبت و اتصال را در تنظیمات آزمایش کنید.")
    try:
        bundled, _ = load_manifest(_EXAMPLES / "benchmark_smoke.json")
    except (OSError, ValueError):
        st.error("مجموعهٔ نمونهٔ پژوهشی در دسترس نیست.")
        return
    instance_index = st.selectbox(
        "مسئلهٔ ساخت‌یافته", range(len(bundled.instances)),
        format_func=lambda index: bundled.instances[index].instance_id,
    )
    baseline = st.selectbox("سیاست پایه", list(_BASELINES), format_func=_BASELINES.__getitem__)
    advisor = st.selectbox("سیاست دارای مشاور", list(_MODEL_POLICIES), format_func=_MODEL_POLICIES.__getitem__)
    seeds = st.multiselect("بذرهای مجموعهٔ نمونه", bundled.master_seeds, default=bundled.master_seeds[:1])
    st.caption(f"سقف هر اجرا {bundled.budget:,} FE است. برای این مقایسه حداکثر {2 * len(seeds)} درخواست مشاور ممکن است هزینهٔ API داشته باشد.")
    if profile:
        st.caption(f"مقصد API برای خلاصهٔ شواهد · {profile['base_url']} · {profile['model']}")
    if st.button("ثبت مقایسه و ارسال به صف", type="primary", disabled=not ready or not seeds):
        try:
            manifest = Manifest.model_validate(bundled.model_dump(mode="json") | {
                "suite_id": "continuous-v1-ui-comparison",
                "provider_profile_revision": {"profile_id": profile["profile_id"], "revision": profile["revision"]},
                "instances": [bundled.instances[instance_index].model_dump(mode="json")],
                "policy_ids": [baseline, advisor],
                "master_seeds": sorted(seeds),
            })
            planned = plan_jobs(manifest, _EXAMPLES)
        except (OSError, ValueError):
            st.error("مدل یا تنظیمات مقایسه با manifest تثبیت‌شده سازگار نیست.")
            return
        manifest_data = manifest.model_dump(mode="json")
        digest = sha256(canonical_json(manifest_data).encode("utf-8")).hexdigest()
        state = st.session_state.get("research_comparison")
        if not state or state["manifest_sha256"] != digest:
            state = {
                "manifest_sha256": digest,
                "manifest": manifest_data,
                "keys": [str(uuid4()) for _ in planned],
                "jobs": [],
            }
            st.session_state.research_comparison = state
        for index in range(len(state["jobs"]), len(planned)):
            instance, job = planned[index]
            queued = attempt("POST", "/jobs", job.model_dump(mode="json"), idempotency_key=state["keys"][index])
            if queued is None:
                st.warning("درخواست‌های ثبت‌شده در تاریخچه حفظ شده‌اند. برای ادامه، همین تنظیمات را دوباره ثبت کنید.")
                break
            state["jobs"].append({
                "instance_id": instance.instance_id,
                "policy": job.policy.selection_policy,
                "master_seed": job.policy.master_seed,
                "job_id": queued["job_id"],
            })
    state = st.session_state.get("research_comparison")
    if not state:
        return
    st.download_button(
        "دریافت manifest و شناسه‌های اجرا",
        canonical_json({key: value for key, value in state.items() if key != "keys"}).encode("utf-8"),
        "comparison.json", "application/json",
    )
    st.button("به‌روزرسانی وضعیت مقایسه")
    for entry in state["jobs"]:
        job_id = entry["job_id"]
        status = attempt("GET", f"/jobs/{job_id}")
        if status:
            st.code(
                f"{entry['policy']} · seed={entry['master_seed']} · "
                f"{status['job_status']} · {status['solution_status']} · {job_id}",
                language=None,
            )
        if st.button("انتخاب اجرا برای مشاهدهٔ گزارش", key=f"research-{job_id}"):
            if open_execution is None:
                st.session_state.job_id = job_id
                st.success("اجرا برای مشاهدهٔ گزارش انتخاب شد.")
            else:
                open_execution(job_id)
