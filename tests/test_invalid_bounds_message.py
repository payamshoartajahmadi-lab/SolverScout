"""Q10's post-conversion error message stays specific and uses only validated names."""
from copy import deepcopy

import pytest

from p_mc_optimizer.workflow.graph import Workflow
from test_llm_gateway import _PROFILE_SETTINGS, _TEST_KEY, checked
from test_llm_workflow import configure, fixture_transport, local_api, submit, work


@pytest.mark.parametrize("name", ["x1", "<script>unsafe_name</script>"])
def test_inconsistent_bounds_message_uses_validated_variable_name_and_zero_fe(tmp_path, monkeypatch, name):
    text = "تابع x1**2+x2**2 را کمینه کن. x1 و x2 حقیقی هستند. کران پایین x1 برابر 5 و کران بالا -5 است. کران x2 از -3 تا 3 است."
    parsed = {
        "extracted_fields": {"variables":[{"name":name,"lower":5.0,"upper":-5.0},
                                          {"name":"x2","lower":-3.0,"upper":3.0}]},
        "missing_fields":[],"ambiguities":[],"source_map":[],"normalizations":[],
        "unsupported_features":[{"kind":"inconsistent_bounds"}],
    }

    def controlled_formulation(self, role, context, **kwargs):
        assert role == "formulator", "No other language role is expected for invalid input"
        return deepcopy(parsed), None

    monkeypatch.setattr(Workflow, "_invoke", controlled_formulation)
    calls=[]
    with local_api(tmp_path, fixture_transport([{"ok":True}], calls)) as (repo, client, address, token):
        profile,_=configure(client,_PROFILE_SETTINGS,_TEST_KEY)
        job_id=submit(client,profile,text)
        work(repo,address,token,job_id)
        state=checked(client.get(f"/jobs/{job_id}"))
        assert state["job_status"] == "failed"
        assert state["error"]["error_code"] == "UNSUPPORTED_PROBLEM"
        if name=="x1":
            assert state["error"]["message_fa"] == "کران پایین x1 از کران بالای آن بزرگ‌تر است. کران‌های مسئله را اصلاح کنید."
        else:
            assert state["error"]["message_fa"] == "کران پایین یکی از متغیرها از کران بالای آن بزرگ‌تر است. کران‌های مسئله را اصلاح کنید."
            assert name not in state["error"]["message_fa"]
        assert state["budget"]["known_started"] == state["budget"]["reserved_active"] == state["budget"]["charged_unknown"] == 0
        assert repo.list_executions(job_id) == []
        assert calls == ["connection_test_v1"]
