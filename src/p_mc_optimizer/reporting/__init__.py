"""Export numerical evidence without altering the report's values."""

from __future__ import annotations

import csv
import html
import io
import json
import math
from collections.abc import Iterable
from typing import Any

CLAIM_FA = "بهترین جواب پیدا‌شده در بودجهٔ اجرا؛ بهینگی سراسری اثبات نشده"
EVALUATION_COLUMNS = (
    "job_id", "execution_id", "trial_id", "attempt_id", "stage_execution_id", "evaluation_id",
    "sequence", "phase", "evaluation_kind", "x", "objective_raw", "score_for_minimization",
    "status", "evaluation_seconds", "problem_math_hash", "evaluator_version", "error_code",
)
TRIAL_COLUMNS = (
    "trial_id", "attempt_id", "phase", "algorithm_id", "config_id", "replicate_index", "seed",
    "evaluation_quota", "execution_status", "termination_reason", "started_evaluations",
    "successful_evaluations", "failed_evaluations", "objective_raw", "score_for_minimization",
    "elapsed_compute_seconds", "error_code",
)


def json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, allow_nan=False).encode()


def csv_bytes(rows: Iterable[dict], columns: tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_cell(row.get(key)) for key in columns})
    return stream.getvalue().encode("utf-8")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or
                                  value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def evaluation_records(path):
    with path.open(encoding="utf-8") as lines:
        for line in lines:
            row = json.loads(line)
            if row.get("kind") == "result":
                yield row["record"]
            elif "evaluation_id" in row and "status" in row:
                yield row


def add_progress(history: Iterable[tuple[Iterable[dict], int, str]]) -> list[dict]:
    """Use the evaluation sequence (including failed calls) and stage offsets."""
    best = math.inf
    points = []
    offset = 0
    for records, started, phase in history:
        if phase != "verification":
            for record in records:
                score = record.get("score_for_minimization")
                if score is not None and score < best:
                    best = score
                    points.append({"started_evaluations": offset + record["sequence"],
                                   "score_for_minimization": score,
                                   "evaluation_id": record["evaluation_id"]})
        offset += started
    if points and points[-1]["started_evaluations"] < offset:
        points.append({**points[-1], "started_evaluations": offset})
    return points


def progress_figure(points: list[dict]) -> str:
    if not points:
        return "<p>تاریخچهٔ کافی برای نمودار در دسترس نیست.</p>"
    scores = [p["score_for_minimization"] for p in points]
    scale = max(1, max(map(abs, scores)))
    hi, lo = max(scores) / scale, min(scores) / scale
    extent = hi - lo or 1
    end = points[-1]["started_evaluations"] or 1
    stride = max(1, len(points) // 1000)
    vertices = []
    previous_y = None
    for point in [*points[::stride], points[-1]]:
        x = 106 + point["started_evaluations"] / end * 600
        y = 20 + (hi - point["score_for_minimization"] / scale) / extent * 175
        if previous_y is not None:
            vertices.append(f"{x:.2f},{previous_y:.2f}")
        vertices.append(f"{x:.2f},{y:.2f}")
        previous_y = y
    top_label, bottom_label = format(max(scores), ".5g"), format(min(scores), ".5g")
    return f"""<figure style="margin:0">
<figcaption>بهترین امتیاز کمینه‌سازی بر حسب تعداد فراخوانی آغازشدهٔ هدف (FE)</figcaption>
<svg viewBox="0 0 735 235" style="width:100%;max-height:320px;direction:ltr"
 role="img" aria-label="نمودار بهترین امتیاز بر حسب FE" font-size="13">
<path d="M106 20 V198 H706" fill="none" stroke="#94a3b8"/>
<polyline points="{' '.join(vertices)}" stroke="#096c61" fill="none" stroke-width="2"/>
<text x="98" y="24" text-anchor="end" fill="#64748b">{top_label}</text>
<text x="98" y="198" text-anchor="end" fill="#64748b">{bottom_label}</text>
<text x="106" y="221" text-anchor="middle" fill="#64748b">0</text>
<text x="706" y="221" text-anchor="middle" fill="#64748b">{end}</text>
</svg></figure>"""


def html_bytes(report: dict) -> bytes:
    def escaped(value: Any) -> str:
        return html.escape("—" if value is None else str(value), quote=True)

    details = []
    labels = {
        "input": "ورودی مسئله", "problem_revision": "مدل تثبیت‌شده و منشأ فیلدها",
        "policy": "سیاست اجرا", "candidate_plan": "پیشنهاد و کاندیداها", "decision": "معیار و شواهد انتخاب",
        "profile": "نمونه‌برداری مسئله", "verification": "اعتبارسنجی مجدد",
        "diagnostics": "اطلاعات عیب‌یابی", "budget": "حساب بودجه", "timing": "زمان‌ها",
        "environment": "محیط و نسخه‌ها", "llm_calls": "فراخوانی‌های مدل و مصرف", "error": "خطای اجرا",
    }
    if report.get("schema_version") == "2.0":
        labels.update({
            "research_snapshot": "منابع و تنظیمات ثابت پژوهش",
            "query_plan": "پرس‌وجوها",
            "research": "اسناد، شواهد و حساب پژوهش",
            "research_error": "محدودیت پژوهش",
            "advisor_plan_v2": "پیشنهادهای مستند و امکان اجرا",
            "catalog_exclusions": "فیلتر کاتالوگ",
            "effective_selection_policy": "سیاست مؤثر",
        })
    for key, label in labels.items():
        if report.get(key) is not None:
            data = escaped(json_bytes(report[key]).decode())
            details.append(f'<details open><summary>{label}</summary><pre dir="ltr">{data}</pre></details>')
    trial_rows = []
    display_columns = ("algorithm_id", "phase", "seed", "started_evaluations", "objective_raw",
                       "score_for_minimization", "execution_status")
    for trial in report.get("trials", []):
        trial_rows.append("<tr>" + "".join(f"<td>{escaped(trial.get(column))}</td>"
                                          for column in display_columns) + "</tr>")
    headings = ("روش", "مرحله", "بذر", "FE", "هدف اصلی", "امتیاز کمینه‌سازی", "وضعیت")
    explain = report.get("explanation") or {}
    limits = "".join(f"<li>{escaped(line)}</li>" for line in explain.get("limitations_fa", []))
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>گزارش بهینه‌سازی P_MC</title>
<style>
body {{font:16px/1.8 system-ui,sans-serif;color:#18322d;background:#f4f8f6;margin:0;padding:24px}}
main {{max-width:1040px;margin:auto}} section,details,figure {{background:white;border:1px solid #ccdcd5;
border-radius:12px;padding:16px;margin:12px 0}} pre {{overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}}
td,th {{padding:8px;text-align:start;border-bottom:1px solid #ccdcd5}} td,code {{direction:ltr;unicode-bidi:isolate}}
svg {{width:100%;max-height:320px;direction:ltr}} .table {{overflow:auto}} summary {{font-weight:600;cursor:pointer}}
</style><main><h1>گزارش بهینه‌سازی P_MC</h1><p>{CLAIM_FA}</p>
<section>
<div>وضعیت اجرا <code>{escaped(report.get("job_status"))}</code></div>
<div>اعتبار جواب <code>{escaped(report.get("solution_status"))}</code></div>
<div>روش منتخب <code>{escaped(report.get("selected_algorithm_id"))}</code></div>
<div>منشأ جواب <code>{escaped(report.get("solution_origin"))}</code></div>
<div>بردار جواب تأییدشده <code>{escaped(report.get("best_x"))}</code></div>
<div>مقدار اصلی تابع هدف <code>{escaped(report.get("objective_raw"))}</code></div>
</section>
{progress_figure(report.get("progress", []))}
<section class="table"><h2>اجرای الگوریتم‌ها</h2><table><thead><tr>
{"".join(f"<th>{heading}</th>" for heading in headings)}</tr></thead>
<tbody>{"".join(trial_rows)}</tbody></table></section>
<section><h2>{'توضیح مدل' if report.get('explanation_status') == 'generated' else 'توضیح قالبی برنامه'}</h2>
<p>{escaped(explain.get("summary_fa"))}</p>
<p>{escaped(explain.get("selection_explanation_fa"))}</p>
<ul>{limits}</ul><p><code>{escaped(report.get("explanation_status"))}</code></p></section>
{"".join(details)}
</main></html>""".encode()


def template_explanation(report):
    """Explain the recorded outcome when no model explanation is available."""
    solution = report["solution_status"]
    summary = {
        "valid_best_found": "دو محاسبهٔ تازه روی نقطهٔ ثبت‌شده، مقدار تابع هدف را در تلرانس مقرر تأیید کردند.",
        "no_valid_solution": "اجرای ثبت‌شده نقطهٔ معتبری برای گزارش به‌عنوان جواب فراهم نکرد.",
        "validation_failed": "بررسی مجدد نقطهٔ ثبت‌شده با معیار اعتبارسنجی سازگار نبود.",
        "validation_incomplete": "بررسی مجدد کامل نشد. نقطهٔ مشاهده‌شده در بخش عیب‌یابی قرار دارد.",
    }[solution]
    selected = report["selected_algorithm_id"]
    decision = report["decision"] or {}
    if decision.get("selection_status") == "insufficient_evidence":
        selection = "کمتر از دو روش، هر سه اجرای پایلوت معتبر را داشتند. اجرای اصلی آغاز نشد."
    elif selected and decision.get("decision_source") == "empirical_pilot_selection":
        selection = (f"روش {selected} با ترتیب میانهٔ امتیاز، IQR نرمال‌شده و شناسهٔ روش "
                     "برای اجرای اصلی انتخاب شد. نتایج هر بذر در جدول آمده‌اند.")
    elif selected:
        selection = f"روش {selected} بر اساس سیاست ثبت‌شده برای اجرای اصلی تعیین شد."
    else:
        selection = None
    limitations = ["بهینگی سراسری در نسخهٔ اول اثبات نمی‌شود."]
    if report["proposal_source"] == "deterministic_fallback":
        limitations.append("پس از پاسخ ساختاری نامعتبر مشاور، برنامهٔ ثابت جایگزین به کار رفت.")
    if report["partial_result"]:
        limitations.append("جزئیات توقف یا شکست مرحله در وضعیت اجرا و اطلاعات عیب‌یابی ثبت شده‌اند.")
    return {"summary_fa": summary, "selection_explanation_fa": selection, "limitations_fa": limitations,
            "evidence_refs": []}
