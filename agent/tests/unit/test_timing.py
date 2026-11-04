from agent.api.timing import summarise


def model(agent, seconds, tokens=100):
    return {"event": "model", "agent": agent, "seconds": seconds, "tokens": tokens}


def tool(agent, name, seconds, ok=True, result=None):
    return {"event": "tool_end", "agent": agent, "tool": name, "seconds": seconds, "ok": ok, "result": result}


def delegate(subagent):
    return {"event": "tool_start", "agent": "main", "tool": "task", "detail": f"{subagent}: do it"}


def test_time_is_split_between_the_model_and_tools():
    timing = summarise([model("main", 30), tool("main", "load_skill", 2), model("verifier", 10), tool("verifier", "x", 8)], 60)
    assert timing["model_seconds"] == 40
    assert timing["tool_seconds"] == 10
    assert timing["turns"] == 2
    assert timing["tokens"] == 200
    assert timing["agents"]["verifier"]["model_seconds"] == 10


def test_a_delegation_is_not_counted_twice():
    timing = summarise([model("sql_author", 10), tool("main", "task", 12)], 12)
    assert timing["tool_seconds"] == 0


def test_the_slowest_steps_come_first():
    timing = summarise([model("main", 3), tool("verifier", "render_dashboard_screenshot", 40), model("sql_author", 20)], 70)
    assert [step["seconds"] for step in timing["slowest"]] == [40, 20, 3]


def test_repair_rounds_are_counted_from_the_verifier():
    timing = summarise([delegate("verifier"), delegate("sql_author"), delegate("verifier"), delegate("verifier")], 1)
    assert timing["repair_rounds"] == 2
    assert timing["delegations"] == {"verifier": 3, "sql_author": 1}


def test_the_explanation_says_where_the_time_went():
    text = " ".join(summarise([model("main", 90), tool("main", "x", 10)], 100)["explanation"])
    assert "90% of the working time was waiting for the model" in text
    assert "straight through" in text


def test_the_explanation_names_repairs_and_failures():
    events = [model("main", 10), delegate("verifier"), delegate("verifier"), tool("main", "x", 1, ok=False, result="FAILED: bad"),
              {"event": "tool_blocked", "agent": "main", "tool": "task"}]
    timing = summarise(events, 20)
    text = " ".join(timing["explanation"])
    assert "repair round" in text and "1 tool call failed" in text and "run limit stopped" in text
    assert "FAILED: bad" in timing["failures"][0]


def test_an_empty_run_explains_nothing():
    assert summarise([], 0)["explanation"] == []
