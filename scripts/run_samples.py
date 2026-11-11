"""Run the README's sample queries two ways and save both answers plus the agent's trace to docs/samples/.

    uv run scripts/run_samples.py          # all three
    uv run scripts/run_samples.py 2        # just sample 2

1. Baseline: the same model (gemini-3.5-flash) with no tools and no system prompt.
2. Agent: the full Lead -> Researchers -> Writer pipeline.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the repo root, for app and storefront

from app import ResearchHooks, check_citations, deep_research  # noqa: E402
from storefront import web  # noqa: E402
from storefront.context_panel import AgentContexts  # noqa: E402

SAMPLES = [
    "On Myrtle Ave, what kinds of businesses that ran 10+ years closed since 2015, and what replaced them?",
    "Which pilot corridors lost the most laundromats and delis between 2013 and today?",
    "Do corridors with the biggest rent increases have higher turnover of long-running businesses?",
]
OUT = Path(__file__).resolve().parent.parent / "docs" / "samples"


def baseline(question: str) -> str:
    resp = web._client().models.generate_content(model="gemini-3.5-flash", contents=question)
    return resp.text


async def agent(question: str) -> tuple[str, list[dict], list[str], float]:
    activity: list[str] = []
    hooks = ResearchHooks(AgentContexts(), lambda e: activity.append(f"{e['agent']}: {e['text']}") if e["type"] == "activity" else None)
    t = time.time()
    report, state = await deep_research(question, "", hooks)
    problems = check_citations(report, state.evidence.ids())
    activity.append(f"citation check: {problems or 'ok'}")
    return report, state.tool_calls, activity, time.time() - t


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    picks = [int(a) for a in sys.argv[1:]] or [1, 2, 3]
    for i in picks:
        q = SAMPLES[i - 1]
        print(f"[{i}] baseline ...", flush=True)
        base = baseline(q)
        print(f"[{i}] agent ...", flush=True)
        report, calls, activity, secs = await agent(q)
        trace = "\n".join(
            f"| {n} | `{c['agent']}` | `{c['name']}` | `{json.dumps(c['arguments'])[:160]}` | {c['evidence_id'] or ''} |"
            for n, c in enumerate(calls, 1))
        (OUT / f"sample{i}.md").write_text(
            f"# Sample {i}\n\n**Question:** {q}\n\n"
            f"## Baseline: gemini-3.5-flash, no tools, no system prompt\n\n{base}\n\n"
            f"## Agent ({secs:.0f}s, {len(calls)} tool calls)\n\n{report}\n\n"
            f"## Agent trace\n\n| # | agent | tool | arguments | evidence |\n|---|---|---|---|---|\n{trace}\n\n"
            f"<details><summary>Activity log</summary>\n\n```\n" + "\n".join(activity) + "\n```\n</details>\n")
        (OUT / f"sample{i}.trace.json").write_text(json.dumps(calls, indent=1, default=str))
        print(f"[{i}] done in {secs:.0f}s, {len(calls)} tool calls", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
