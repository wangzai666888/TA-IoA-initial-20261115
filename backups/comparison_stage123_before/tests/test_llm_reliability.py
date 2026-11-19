"""Regressions from external-LLM logs; no rule-planner substitution."""
from copy import deepcopy
import json
import httpx
import pytest

from co_ioa.errors import ModelServiceError
from co_ioa.model_api import ModelAPI
from co_ioa.runner import run_experiment
from co_ioa.scenarios import suite


ENV = {"CO_IOA_API_BASE": "https://provider.example/v1", "CO_IOA_MODEL": "test-model",
       "CO_IOA_API_KEY": "private-key", "CO_IOA_MODEL_TIMEOUT": "1",
       "CO_IOA_MODEL_RETRY_BACKOFF": "0"}


def response(tasks):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
        {"requirements": tasks, "change_summary": "model proposal"})}}]})


def context(request):
    body = json.loads(request.content)
    return json.loads(body["messages"][1]["content"].split("\nCorrection required:")[0])


def install_transport(monkeypatch, handler):
    monkeypatch.setattr("co_ioa.model_api.ModelAPI", lambda audit: ModelAPI(
        audit, environment=ENV, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("name", ["feasible_shift", "remote_server_parallel", "authorized_revision"])
def test_unrequested_analysis_is_rejected_then_model_corrects(monkeypatch, tmp_path, name):
    scenario = suite()[name]
    calls = []
    def respond(request):
        ctx = context(request)
        calls.append(ctx)
        tasks = deepcopy(scenario["requirements"])
        if len(calls) == 1:
            tasks.append({"task_id": "analyse_sonar", "capability_description": "analysis",
                "earliest_start": 0, "deadline": 900, "duration": 30, "dependencies": ["inspect"],
                "parameters": {}, "required": True})
        if ctx["trigger_event"]["type"] == "matching_failed":
            tasks[0]["deadline"] = 1200
        return response(tasks)
    install_transport(monkeypatch, respond)
    result = run_experiment(scenario, tmp_path, backend="offline", planner="llm")
    assert result["outcome"] == "success"
    assert set(result["tasks"]) == {t["task_id"] for t in scenario["requirements"]}
    assert result["planning_attempts"][0]["status"] == "rejected"
    assert "extra=" in result["planning_attempts"][0]["reason"]
    assert len(result["model_calls"]) == len(calls)
    assert "private-key" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["required", "deadline", "omission"])
def test_initial_task_contract_is_checked_before_any_dispatch(monkeypatch, tmp_path, mutation):
    scenario = suite()["objective_revision_parallel"]
    def respond(request):
        tasks = deepcopy(scenario["requirements"])
        if mutation == "omission": tasks.pop()
        elif mutation == "required": tasks[0]["required"] = False
        else: tasks[0]["deadline"] = 800
        return response(tasks)
    install_transport(monkeypatch, respond)
    result = run_experiment(scenario, tmp_path, backend="offline", planner="llm")
    assert result["outcome"] == "error"
    assert result["error_kind"] == "planning_validation"
    assert not result["evaluation_valid"]
    assert result["metrics"]["collaboration_success_rate"] is None
    assert not result["executions"]
    assert len(result["planning_attempts"]) == 3


@pytest.mark.parametrize("backend", ["offline", "ioa"])
def test_objective_revision_alias_and_transient_timeout_preserve_running_work(monkeypatch, tmp_path, backend):
    scenario = suite()["objective_revision_parallel"]
    attempts = []
    def respond(request):
        ctx = context(request)
        attempts.append(ctx)
        if len(attempts) == 2:
            raise httpx.ReadTimeout("provider secret must not be logged", request=request)
        tasks = deepcopy(ctx["trigger_event"].get("requirements", scenario["requirements"]))
        # Reverse the equivalent spellings between P rounds, including RUNNING work.
        if ctx["trigger_event"]["type"] == "initial":
            for task in tasks:
                if task["task_id"] == "inspect": task["capability_description"] = "sonar"
                if task["task_id"] == "parallel": task["capability_description"] = "surface survey"
        return response(tasks)
    install_transport(monkeypatch, respond)
    result = run_experiment(scenario, tmp_path, backend=backend, planner="llm")
    assert result["outcome"] == "success"
    assert result["tasks"]["parallel"]["actual_start_time"] == 0
    assert result["tasks"]["parallel"]["actual_end_time"] == 400
    assert result["tasks"]["extra"]["actual_start_time"] == 400
    assert result["calls"]["P"] == 2
    assert len(result["model_calls"]) == 3
    assert result["model_calls"][1]["retry_scheduled"]
    assert result["model_calls"][2]["read_timeout_seconds"] == 2
    assert [r["status"] for r in result["planning_attempts"]] == ["accepted", "accepted"]
    assert "provider secret" not in json.dumps(result)


def test_exhausted_service_timeout_is_error_not_collaboration_failure(monkeypatch, tmp_path):
    scenario = suite()["objective_revision_parallel"]
    def respond(request):
        if context(request)["trigger_event"]["type"] == "initial":
            return response(scenario["requirements"])
        raise httpx.ReadTimeout("provider secret", request=request)
    install_transport(monkeypatch, respond)
    result = run_experiment(scenario, tmp_path, backend="offline", planner="llm")
    assert result["outcome"] == "error" and result["ended_at"] == 120
    assert result["error_kind"] == "model_service" and not result["evaluation_valid"]
    assert result["metrics"]["on_time_execution_completion_rate"] is None
    assert result["metrics"]["collaboration_success_rate"] is None
    assert len(result["model_calls"]) == 4  # Initial request plus three timed-out attempts.
    assert result["planning_attempts"][-1]["status"] == "service_error"
    assert "provider secret" not in json.dumps(result)


@pytest.mark.parametrize("codes, expected_calls", [([429, 503, 200], 3), ([401], 1)])
def test_retryable_http_statuses_and_authentication_errors(codes, expected_calls):
    calls, audit = [], []
    def respond(request):
        code = codes[len(calls)]
        calls.append(code)
        return response([]) if code == 200 else httpx.Response(code, text="private-key", headers={"Retry-After": "0"})
    model = ModelAPI(audit, environment=ENV, transport=httpx.MockTransport(respond))
    if codes[-1] == 200:
        assert json.loads(model("", {}, ""))["requirements"] == []
    else:
        with pytest.raises(ModelServiceError, match="HTTP 401"):
            model("", {}, "")
    assert len(calls) == expected_calls
    assert "private-key" not in json.dumps(audit)
