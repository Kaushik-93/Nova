from types import SimpleNamespace

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI

from agent.config import Settings
from agent.harness.budgets import BudgetLimits, RunBudget
from agent.harness.errors import BudgetExceeded, InvalidRequest
from agent.harness.middleware import RunMiddleware, SubagentLimits, describe_call
from agent.llm import build_model


def settings(provider: str, name: str) -> Settings:
    return Settings(model_provider=provider, model_name=name)


def test_openai_uses_the_responses_api(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    model = build_model(settings("openai", "gpt-6-luna"))
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "gpt-6-luna"
    assert model.use_responses_api is True


def test_anthropic_is_supported(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    model = build_model(settings("anthropic", "claude-opus-5-5"))
    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-opus-5-5"


def test_an_unknown_provider_is_refused():
    with pytest.raises(InvalidRequest, match="MODEL_PROVIDER"):
        build_model(settings("nope", "x"))


def test_settings_hold_no_llm_key():
    assert not [name for name in Settings.model_fields if "api_key" in name]


def reply(total_tokens: int, reasoning: int = 0, tools=()) -> AIMessage:
    return AIMessage("hi", tool_calls=[{"name": name, "args": {"secret": "value"}, "id": name} for name in tools],
                     usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": total_tokens,
                                     "output_token_details": {"reasoning": reasoning}})


def model_call(middleware, message):
    return middleware.wrap_model_call(object(), lambda request: SimpleNamespace(result=[message]))


def tool_call(middleware, name, result):
    request = SimpleNamespace(tool_call={"name": name, "args": {"sql": "SELECT secret"}})
    return middleware.wrap_tool_call(request, lambda r: result)


def test_each_model_call_is_a_step_and_its_tokens_are_counted():
    budget = RunBudget(BudgetLimits())
    model_call(RunMiddleware(budget), reply(150))
    assert budget.steps == 1
    assert budget.tokens == 150


def test_a_spent_budget_stops_the_next_model_call():
    budget = RunBudget(BudgetLimits(max_tokens=100))
    middleware = RunMiddleware(budget)
    model_call(middleware, reply(150))
    with pytest.raises(BudgetExceeded, match="tokens"):
        model_call(middleware, reply(1))


def test_a_message_without_usage_is_harmless():
    budget = RunBudget(BudgetLimits())
    model_call(RunMiddleware(budget), AIMessage("hi"))
    assert budget.tokens == 0


def test_model_steps_are_traced_with_who_tokens_and_tools():
    events = []
    middleware = RunMiddleware(RunBudget(BudgetLimits()), agent="sql_author", run_id="r1", sink=events.append)
    model_call(middleware, reply(150, reasoning=40, tools=["run_readonly_sql"]))
    assert events[0]["event"] == "model_start", "so the view can show a model call in progress"
    event = events[1]
    assert (event["event"], event["agent"], event["run_id"]) == ("model", "sql_author", "r1")
    assert event["tokens"] == 150
    assert event["reasoning_tokens"] == 40
    assert event["tools"] == ["run_readonly_sql"]


def test_tool_steps_are_traced_with_their_outcome():
    events = []
    middleware = RunMiddleware(RunBudget(BudgetLimits()), sink=events.append)
    tool_call(middleware, "good", ToolMessage("ok", tool_call_id="1"))
    tool_call(middleware, "bad", ToolMessage("FAILED [sql]: nope", tool_call_id="2"))
    assert [(e["event"], e.get("ok")) for e in events] == [("tool_start", None), ("tool_end", True), ("tool_start", None),
                                                           ("tool_end", False)]


def test_arguments_and_text_stay_out_of_the_logs(caplog):
    middleware = RunMiddleware(RunBudget(BudgetLimits()))
    with caplog.at_level("INFO", logger="nova.trace"):
        model_call(middleware, AIMessage("private text"))
        tool_call(middleware, "run_readonly_sql", ToolMessage("private data", tool_call_id="1"))
    logged = " ".join(str(vars(record)) for record in caplog.records)
    assert "private" not in logged and "SELECT secret" not in logged


def test_the_live_view_gets_the_text_and_what_a_tool_is_doing():
    events = []
    middleware = RunMiddleware(RunBudget(BudgetLimits()), sink=events.append)
    model_call(middleware, AIMessage("Checking the totals"))
    tool_call(middleware, "run_readonly_sql", ToolMessage("ok", tool_call_id="1"))
    assert events[1]["text"] == "Checking the totals"
    assert events[2]["detail"] == "SELECT secret"


def test_a_failing_tool_reports_why():
    events = []
    tool_call(RunMiddleware(RunBudget(BudgetLimits()), sink=events.append), "x", ToolMessage("FAILED [sql]: bad", tool_call_id="1"))
    assert events[-1]["result"] == "FAILED [sql]: bad"


@pytest.mark.parametrize("name, args, expected", [
    ("load_skill", {"name": "demo-rental"}, "demo-rental"),
    ("task", {"subagent_type": "verifier", "description": "Check it"}, "verifier: Check it"),
    ("create_chart", {"spec": {"title": "Revenue"}}, "Revenue"),
    ("semantic_query", {"query": {"measures": ["c.revenue"], "dimensions": ["c.region"]}}, "c.revenue, c.region"),
    ("run_readonly_sql", {"sql": "SELECT\n  1"}, "SELECT 1"),
    ("something_else", {"secret": "x"}, ""),
])
def test_tool_calls_are_described_in_a_line(name, args, expected):
    assert describe_call(name, args) == expected


def delegate(middleware, subagent):
    request = SimpleNamespace(tool_call={"name": "task", "id": "1", "args": {"subagent_type": subagent}})
    calls = []
    result = middleware.wrap_tool_call(request, lambda r: calls.append(r) or ToolMessage("done", tool_call_id="1"))
    return result, calls


def test_a_subagent_is_refused_past_its_limit():
    middleware = RunMiddleware(RunBudget(BudgetLimits()), limits=SubagentLimits({"verifier": 2}))
    for _ in range(2):
        assert delegate(middleware, "verifier")[1], "allowed"
    result, calls = delegate(middleware, "verifier")
    assert calls == []
    assert "Finish now" in result.content


def test_limits_are_per_subagent():
    middleware = RunMiddleware(RunBudget(BudgetLimits()), limits=SubagentLimits({"verifier": 1, "sql_author": 1}))
    delegate(middleware, "verifier")
    assert delegate(middleware, "sql_author")[1]
