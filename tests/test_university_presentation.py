"""Offline display/interaction contracts; not evidence of live model quality."""
from copy import deepcopy
from html.parser import HTMLParser
import re
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest
from fastapi.testclient import TestClient

from p_mc_optimizer.reporting import html_bytes, progress_figure
from p_mc_optimizer.reporting.presentation import (
    build_report_view_model, catalog_selection_view, model_usage_view, render_view_model_html,
)
from test_narrative_closure_presentation import fixture, prepare

SIX = ['mealpy:swarm_based.ABC:OriginalABC', 'mealpy:swarm_based.PSO:OriginalPSO',
       'mealpy:evolutionary_based.ES:CMA_ES', 'mealpy:evolutionary_based.DE:OriginalDE',
       'mealpy:swarm_based.GWO:OriginalGWO', 'mealpy:swarm_based.WOA:OriginalWOA']


def test_frozen_six_card_counts_and_non_nominees_are_shared_without_live_catalog(monkeypatch):
    from p_mc_optimizer.catalog.store import CatalogStore
    monkeypatch.setattr(CatalogStore, 'list_implementations', lambda *a, **kw: pytest.fail('Read live catalog'))
    report = fixture()
    report['catalog_selection_summary'] = {'discovered_count': 234, 'qualified_count': 6, 'eligible_count': 6,
                                          'sent_count': 6, 'sent_implementation_ids': SIX, 'catalog_hash': 'a'*64}
    report = prepare(report)
    before = deepcopy(report)
    vm = build_report_view_model(report)
    choices = vm['catalog_selection']
    assert (choices['sent_count'], choices['nominated_count']) == (6, 3)
    assert {row['implementation_id'] for row in choices['not_nominated']} == set(SIX[:3])
    body = render_view_model_html(vm)
    assert body in html_bytes(report).decode()
    assert body.index('data-block="candidate-selection"') < body.index('data-block="comparison"')
    assert 'گزینه‌های ارسال‌شده که نامزد نشدند' in body
    assert 'رتبه یا نتیجهٔ عددی ثبت نشده است' in body
    assert not choices['limited_choice']
    assert report == before


def test_old_three_card_record_is_explicitly_limited_without_inventing_inventory_counts():
    report = fixture()
    choice = catalog_selection_view(report)
    assert choice['sent_count'] == choice['nominated_count'] == 3
    assert choice['limited_choice']
    assert choice['discovered_count'] is choice['qualified_count'] is choice['eligible_count'] is None
    assert 'انتخاب از مجموعه‌ای بزرگ‌تر را نشان نمی‌دهد' in html_bytes(prepare(report)).decode()
    assert catalog_selection_view({})['sent_count'] is None
    report['catalog_selection_summary'] = {'sent_count': None, 'sent_implementation_ids': [], 'sent_status': 'outcome_unknown'}
    assert catalog_selection_view(report)['sent_count'] is None


def test_usage_requires_complete_recorded_counts_and_unknown_cost_stays_unknown():
    calls = [
        {'role': 'explainer', 'status': 'succeeded', 'usage': {'prompt_tokens': 15, 'completion_tokens': 7, 'total_tokens': 22},
         'provenance': {'request_body_bytes': 140}},
        {'role': 'report_reviewer', 'status': 'outcome_unknown', 'usage': None, 'provenance': {'request_body_bytes': 200}},
    ]
    usage = model_usage_view({'llm_calls': calls, 'cost_estimate': None})
    assert usage['recorded_calls'] == 2 and usage['unknown_calls'] == 1
    assert usage['input_tokens'] is usage['output_tokens'] is usage['total_tokens'] is None
    assert usage['roles'][0]['input_tokens'] == 15
    assert usage['roles'][1]['input_tokens'] is None
    assert [row['request_body_bytes'] for row in usage['roles']] == [140, 200]
    assert usage['cost'] == 'ثبت نشده'
    calls[1]['usage'] = {'prompt_tokens': 18, 'completion_tokens': 2, 'total_tokens': 20}
    usage = model_usage_view({'llm_calls': calls})
    assert (usage['input_tokens'], usage['output_tokens'], usage['total_tokens']) == (33, 9, 42)
    calls[0]['usage']['prompt_tokens'] = True
    assert model_usage_view({'llm_calls': calls})['input_tokens'] is None


def test_absent_phase_counts_remain_missing_and_aggregate_scope_is_explicit():
    report = prepare()
    report['trials'] = [row for row in report['trials'] if row['phase'] != 'main']
    vm = build_report_view_model(report)
    assert vm['counts']['main_started'] is None
    assert vm['counts']['pilot_started'] == 4500
    assert 'ممکن است هم‌پوشانی داشته باشند' in render_view_model_html(vm)


@pytest.mark.parametrize('scores', [[100, 1e-12, 0], [10, 0, -2], [0, 0], [1e308, 1e-308, 0]])
def test_signed_log_chart_preserves_original_scores_fe_and_real_zero(scores):
    points = [{'started_evaluations': (index+1)*100, 'score_for_minimization': value}
              for index, value in enumerate(scores)]
    before = deepcopy(points)
    linear = progress_figure(points)
    logarithmic = progress_figure(points, axis_scale='symlog')
    assert 'data-axis-scale="linear"' in linear
    assert 'data-axis-scale="symlog"' in logarithmic
    assert '>0</text>' in logarithmic
    assert f'>{len(scores)*100}</text>' in logarithmic
    assert 'صفر و علامت مقدار واقعی حفظ می‌شوند' in logarithmic
    assert not re.search(r'(?i)(?:[=, ])(?:nan|inf)(?:[ ,"<])', logarithmic)
    assert points == before


def test_shared_chart_control_is_pure_and_presentation_keeps_scalar_numbers_unbroken():
    report = prepare()
    before = deepcopy(report)
    doc = html_bytes(report).decode()
    assert doc.count('<h1>') == 1
    assert 'td bdi{white-space:nowrap}' in doc
    assert 'overflow-x:auto' in doc and 'font-size:1.3rem' in doc
    assert 'data-block="progress-scale"' in doc
    assert 'sign(y)' in doc and '≤' in doc
    assert 'گزارش خودکار بر پایهٔ داده‌های اجرا' in doc
    assert 'متن تأییدشدهٔ مدل در دسترس نیست' in doc
    assert 'هزینهٔ پولی: ثبت نشده' in doc
    assert report == before


def _preparation_page():
    import streamlit as st
    from p_mc_optimizer.ui.preparation import preparation_panel

    def attempt(method, route, payload=None, *, idempotency_key=None):
        assert route == '/demo/preparation'
        if method == 'POST':
            st.session_state.requests.append({'payload': payload, 'key': idempotency_key})
            if len(st.session_state.requests) == 1:
                return None  # Simulate a lost local response, then retry the same operation.
            st.session_state.completed = True
        ready = st.session_state.get('completed', False)
        return {'preparation_id': 'offline-contract', 'status': 'completed' if ready else 'not_started',
                'ready': ready, 'methods': [], 'qualification_budget': 100000,
                'charged_evaluations': 75000 if ready else 0, 'reserved_evaluations': 0,
                'official_source': {'enabled': ready, 'usable_documents': 4 if ready else 0}, 'errors': []}

    preparation_panel(attempt)


def test_prepare_ui_explicit_source_activation_same_api_and_idempotent_retry():
    page = AppTest.from_function(_preparation_page)
    page.session_state['requests'] = []
    page.run()
    assert not page.exception
    assert not page.checkbox[0].value and page.session_state['requests'] == []
    page.checkbox[0].check().run()
    button = next(item for item in page.button if item.label == 'آماده‌سازی نسخهٔ دانشگاهی')
    button.click().run()
    assert not page.exception
    assert len(page.session_state['requests']) == 1
    next(item for item in page.button if item.label == 'آماده‌سازی نسخهٔ دانشگاهی').click().run()
    assert not page.exception
    requests = page.session_state['requests']
    assert requests[0] == requests[1]
    assert requests[0]['payload'] == {'enable_official_source': True, 'qualification_budget': 100000}
    assert requests[0]['key']
    assert any('آماده‌سازی درخواستی کامل شد' in row.value for row in page.success)


def test_empty_update_source_has_persian_preparation_guidance_and_no_empty_select(monkeypatch):
    from p_mc_optimizer.ui import wizard
    messages = []
    monkeypatch.setattr(wizard, 'st', SimpleNamespace(info=messages.append))
    assert wizard._direct_sources({'source_configs': {}}, {'max_sources': 3}) == []
    assert 'آماده‌سازی نسخهٔ دانشگاهی' in messages[0]
    assert 'No options to select' not in messages[0]


def _actual_preparation_page():
    import streamlit as st
    from p_mc_optimizer.ui.preparation import preparation_panel

    def attempt(method, route, payload=None, *, idempotency_key=None):
        headers = {'Idempotency-Key': idempotency_key} if idempotency_key else {}
        response = st.session_state.client.request(method, route, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    preparation_panel(attempt)


def test_prepare_panel_fresh_store_uses_real_api_with_unset_budget_without_qualification(tmp_path):
    from p_mc_optimizer.api import create_app
    app = create_app(tmp_path, 'offline-university-ui-contract')
    with TestClient(app, headers={'X-PMC-Internal-Token': 'offline-university-ui-contract'}) as client:
        initial = client.get('/demo/preparation').raise_for_status().json()
        assert initial['status'] == 'not_started' and initial['qualification_budget'] is None
        page = AppTest.from_function(_actual_preparation_page)
        page.session_state['client'] = client
        page.run()
        assert not page.exception
        assert any('هنوز تعیین نشده' in row.value for row in page.caption)
        assert not page.checkbox[0].value
        after = client.get('/demo/preparation').raise_for_status().json()
        assert after['status'] == 'not_started' and after['attempts'] == []
        assert after['charged_evaluations'] == after['reserved_evaluations'] == 0
