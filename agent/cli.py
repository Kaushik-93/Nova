"""Build a dashboard from the command line, until the UI exists.

    python -m agent.cli --prompt "Show me sales for the last 12 months"
    python -m agent.cli --thread rental-1 --prompt "Add a chart of damages by severity"

A thread is a conversation, and one conversation is one dashboard: pass the same thread to
change what it built.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from agent.config import get_settings
from agent.dashboard_agent import RunStatus, run_dashboard
from agent.db import apply_schema, create_checkpointer, create_langgraph_pool, create_pool, create_store
from agent.harness.audit import RunContext
from agent.harness.logs import configure_logging
from agent.llm import build_model


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a dashboard from a prompt.")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--thread", default="", help="continue an earlier conversation")
    parser.add_argument("--user", default="cli")
    args = parser.parse_args()

    configure_logging("WARNING")
    settings = get_settings()
    thread = args.thread or f"cli-{uuid.uuid4().hex[:10]}"
    pool = create_pool(settings.database_url)
    graph_pool = create_langgraph_pool(settings.database_url)
    apply_schema(pool)
    print(f"Working on it. This usually takes a few minutes. Thread: {thread}", flush=True)
    try:
        result = run_dashboard(settings=settings, pool=pool, model=build_model(settings),
                               checkpointer=create_checkpointer(graph_pool), store=create_store(graph_pool),
                               context=RunContext(run_id=f"cli-{uuid.uuid4().hex[:10]}", user_id=args.user),
                               thread_id=thread, prompt=args.prompt)
    finally:
        pool.close()
        graph_pool.close()

    print(f"\nStatus: {result.status.value}")
    if result.title:
        print(f"Dashboard: {result.title}")
        print(f"Open it: {settings.nova_ui_origin}/dashboards/{result.dashboard_slug}")
    if result.summary:
        print(f"\n{result.summary}")
    for definition in result.definitions:
        print(f"- {definition}")
    for warning in result.warnings:
        print(f"Warning: {warning}")
    if result.question:
        print(f"\nThe agent is asking: {result.question}")
    if result.error:
        print(f"\nError: {result.error}")
    print(f"\nUsed {result.usage.get('tokens', 0):,.0f} tokens in {result.usage.get('elapsed_seconds', 0):.0f}s. "
          f'To change this dashboard: make agent THREAD={thread} PROMPT="..."')
    return 0 if result.status is RunStatus.FINISHED else 1


if __name__ == "__main__":
    sys.exit(main())
