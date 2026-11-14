"""A report-only reproduction must not look like a new numerical solve."""
from copy import deepcopy
from types import SimpleNamespace

from p_mc_optimizer.reporting import html_bytes
from p_mc_optimizer.reporting.evidence_v3 import core_hash
from p_mc_optimizer.reporting.presentation import build_report_view_model, render_view_model_html
from test_narrative_closure_presentation import prepare


def test_report_only_scope_is_shared_by_ui_and_html_without_changing_numeric_core(monkeypatch):
    from p_mc_optimizer.ui import results

    report = prepare()
    before = core_hash(report)
    report['qa_reporting'] = {'schema_version': 'u1-report-replay/1.0', 'scope': 'report_only'}
    original = deepcopy(report)
    view = build_report_view_model(report)
    body = render_view_model_html(view)
    assert 'report-scope' in body and view['report_scope_notice'] in body
    assert 'محاسبات ذخیره‌شده' in view['report_scope_notice']
    assert 'حل‌گر و تابع هدف دوباره اجرا نشده‌اند' in view['report_scope_notice']
    assert body in html_bytes(report).decode('utf-8')
    displayed = []
    monkeypatch.setattr(results, 'st', SimpleNamespace(markdown=lambda text, **kw: displayed.append(text)))
    results._summary(report, {}, None, None, True, 'report-only')
    assert displayed == [body]
    assert report == original and core_hash(report) == before


def test_historical_reports_do_not_acquire_a_new_report_only_claim():
    report = prepare()
    for marker in (None, {'scope': 'report_only'}, {'schema_version': 'u1-report-replay/1.0', 'scope': 'full_solve'}):
        report['qa_reporting'] = marker
        view = build_report_view_model(report)
        assert 'report_scope_notice' not in view
        assert 'report-scope' not in render_view_model_html(view)
