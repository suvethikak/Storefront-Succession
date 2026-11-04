import argparse
import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from agents import Agent, ModelSettings, RunContextWrapper, Runner, function_tool, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

import storefronts as sf
from context_panel import AgentContexts, PanelHooks, add_context_panel

load_dotenv(Path(__file__).parent / ".env")

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# LiteLLM routes the call to Gemini. api_key is unused: Vertex AI uses your gcloud credentials.
MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash", api_key="unused")
# Researchers only query and take notes: the cheaper model is enough.
RESEARCHER_MODEL = LitellmModel(model="vertex_ai/gemini-3.5-flash-lite", api_key="unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"vertex_location": "global", "timeout": 90, "num_retries": 2})

# The Lead gets this many researchers in all. Prompts ask for restraint; code enforces it.
MAX_RESEARCHERS = 6
LEAD_MAX_TURNS = 10
RESEARCHER_MAX_TURNS = 10


@dataclass
class Research:
    """One run's state, shared by every agent and tool in it. The model never sees it."""

    hooks: PanelHooks
    researchers: int = 0  # how many researchers have started, to number them
    findings: list[str] = field(default_factory=list)  # what each researcher reported, for the Writer
    evidence: sf.EvidenceLog = field(default_factory=sf.EvidenceLog)  # E1, E2, ... behind every citation
    tool_calls: list[dict] = field(default_factory=list)  # every tool call by every agent, for /chat


def agent_field(name: str) -> str:
    """'Licenses Researcher 2' -> 'licenses_researcher', 'Lead' -> 'lead'."""
    return re.sub(r"_\d+$", "", re.sub(r"\W+", "_", name.strip().lower()))


class ResearchHooks(PanelHooks):
    """The starter's context panel hooks, plus a record of every tool call with the agent that made it."""

    async def on_tool_end(self, context, agent, tool, result):
        ctx = context.context
        if isinstance(ctx, Research):
            output = str(result)
            ctx.tool_calls.append({
                "agent": agent_field(agent.name), "agent_name": agent.name, "name": tool.name,
                "arguments": _json_or_str(getattr(context, "tool_arguments", None)),
                "evidence_id": _evidence_id(output), "output": output[:4000],
            })
        self.activity(agent.name, f"{tool.name} returned" + (f" [{_evidence_id(str(result))}]" if _evidence_id(str(result)) else ""))


def _json_or_str(s):
    try:
        return json.loads(s) if s else {}
    except (TypeError, ValueError):
        return s


def _evidence_id(output: str) -> str | None:
    m = re.match(r'\{"evidence_id": "(E\d+)"', output)
    return m.group(1) if m else None


# --- Data tools: each stamps its result with an evidence ID before the model sees it ---


def _cite(ctx: RunContextWrapper[Research], result: dict) -> str:
    return sf.to_json(ctx.context.evidence.add(result))


@function_tool
def list_corridors(ctx: RunContextWrapper[Research]) -> str:
    """List the pilot corridors this data covers, with ZIPs, number of businesses, and years covered."""
    return _cite(ctx, sf.list_corridors())


@function_tool
def corridor_summary(ctx: RunContextWrapper[Research], corridor: str, start_year: int = 2013, end_year: int = 2025) -> str:
    """Per business category on one corridor: how many were active in start_year vs end_year, and how many
    opened, closed, and closed after 10+ years in between.

    Args:
        corridor: Corridor key or name, e.g. 'myrtle_ave' or 'Nostrand Ave'.
        start_year: First year of the window.
        end_year: Last year of the window.
    """
    return _cite(ctx, sf.corridor_summary(corridor, start_year, end_year))


@function_tool
def find_closures(ctx: RunContextWrapper[Research], corridor: str, category: str | None = None,
                  min_years: float = 10, since_year: int | None = None) -> str:
    """Businesses on a corridor that closed after running at least min_years, newest first, with what replaced each.

    Args:
        corridor: Corridor key or name.
        category: Optional business category, e.g. 'bodega_deli', 'laundromat_drycleaner', 'restaurant', 'chain'.
        min_years: Minimum years open (10 = long-running).
        since_year: Only closures in or after this year.
    """
    return _cite(ctx, sf.find_closures(corridor, category, min_years, since_year))


@function_tool
def category_trend(ctx: RunContextWrapper[Research], corridor: str, category: str,
                   start_year: int = 2005, end_year: int = 2025) -> str:
    """Year by year: how many businesses of one category were active, opened, and closed on a corridor.

    Args:
        corridor: Corridor key or name.
        category: Business category, e.g. 'laundromat_drycleaner' or 'bodega_deli'.
        start_year: First year.
        end_year: Last year.
    """
    return _cite(ctx, sf.category_trend(corridor, category, start_year, end_year))


@function_tool
def storefront_timeline(ctx: RunContextWrapper[Research], address: str) -> str:
    """Every known occupant of one storefront address, in order: licensed businesses and registry vacancy spells.

    Args:
        address: House number and corridor street, e.g. '1372 Nostrand Ave' or '76-01 Roosevelt Ave'.
    """
    return _cite(ctx, sf.storefront_timeline(address))


@function_tool
def replacements(ctx: RunContextWrapper[Research], corridor: str, closed_category: str | None = None,
                 since_year: int | None = None, long_running_only: bool = True) -> str:
    """What replaced closed businesses on a corridor: counts by (closed category -> replacement category), with examples.

    Args:
        corridor: Corridor key or name.
        closed_category: Optional category of the businesses that closed.
        since_year: Only closures in or after this year.
        long_running_only: Only businesses that had run 10+ years.
    """
    return _cite(ctx, sf.replacements(corridor, closed_category, since_year, long_running_only))


@function_tool
def vacancy_trend(ctx: RunContextWrapper[Research], corridor: str) -> str:
    """Storefront vacancy rate on a corridor for each storefront-registry year (2019 onward).

    Args:
        corridor: Corridor key or name.
    """
    return _cite(ctx, sf.vacancy_trend(corridor))


@function_tool
def rent_income_change(ctx: RunContextWrapper[Research], corridor: str) -> str:
    """Census ACS median rent, household income and population around a corridor: 2011-2015 vs the latest 5 years.

    Args:
        corridor: Corridor key or name.
    """
    return _cite(ctx, sf.rent_income_change(corridor))


@function_tool
def compare_corridors(ctx: RunContextWrapper[Research], metric: str = "long_running_closure_rate", since_year: int = 2014) -> str:
    """All pilot corridors side by side: long-running closure rate, annual turnover, rent and income change, vacancy.

    Args:
        metric: Sort by one of long_running_closure_rate, annual_turnover_rate, long_running_closures,
            rent_pct_change, income_pct_change, vacancy_latest.
        since_year: Start of the closure window.
    """
    return _cite(ctx, sf.compare_corridors(metric, since_year))


@function_tool
def run_sql(ctx: RunContextWrapper[Research], query: str) -> str:
    """Read-only SQL (one SELECT) over the DuckDB tables, for questions the other tools don't cover.
    Tables: businesses, closures, storefront_timeline, records, registry, vacancy, acs, corridors.

    Args:
        query: A single SELECT statement.
    """
    return _cite(ctx, sf.run_sql(query))


# --- The researcher: one shape, one config per data source ---

RESEARCHER_BASE = """\
You research one task about storefront turnover on NYC commercial corridors, using only your tools.

- Use at most 6 tool calls. Start with the tool that fits the task best; don't repeat a call with the same arguments.
- Every tool result has an evidence_id (E1, E2, ...) and a note about what the data covers. Read the note.
- Reply with at most 8 bullets of facts that answer your task. End every bullet with the evidence ID(s) it came
  from, e.g. [E3] or [E3, E5]. Use exact numbers, names, addresses and years from the rows.
- End with one bullet starting "Not answered:" for any part of the task the data couldn't answer, and why
  (e.g. a coverage limit from a note). If everything was answered, say "Not answered: nothing".
- Use the project's definitions; never redefine them in your own SQL: long-running = open 10+ years
  (closures.long_running), closed = every license inactive, turnover = closures / active businesses.
- When comparing corridors, compare rates (closures relative to the number of businesses), never raw counts:
  corridors differ in size.
- Never guess or use outside knowledge. Never say why a business closed: the data shows when, not why."""

SPECIALTIES = {
    "licenses": {
        "label": "Licenses",
        "about": "business licenses (city DCWP, NYS food stores, NYS liquor): which businesses opened and closed, how long they ran",
        "tools": [list_corridors, corridor_summary, find_closures, category_trend, run_sql],
        "focus": "You cover the license data: which kinds of businesses closed, how long they had run, and how category "
                 "counts changed over time. Use find_closures for named examples and corridor_summary or category_trend for counts.",
    },
    "storefront": {
        "label": "Storefront",
        "about": "what replaced closed businesses at the same address, single-address timelines, and storefront vacancy (2019+)",
        "tools": [replacements, storefront_timeline, find_closures, vacancy_trend, run_sql],
        "focus": "You cover what happened to the storefronts: what replaced closed businesses (replacements), the chain of "
                 "occupants at specific addresses (storefront_timeline), and vacancy (vacancy_trend). For a couple of "
                 "telling examples, look up their addresses with storefront_timeline.",
    },
    "demographics": {
        "label": "Demographics",
        "about": "Census rent, income and population change around each corridor, and cross-corridor comparisons",
        "tools": [rent_income_change, compare_corridors, vacancy_trend],
        "focus": "You cover rent, income and population change (ACS) and comparisons across corridors. Report changes "
                 "with both values and the % change. For any rent-vs-turnover question, call compare_corridors and report, "
                 "for every corridor, rent_pct_change, long_running_closure_rate and annual_turnover_rate, plus the Spearman "
                 "values from its SUMMARY row. Say plainly whether the ranks line up, and that 5 corridors is too few to be "
                 "conclusive. Relationships between rent and turnover are correlation only.",
    },
}

researcher = Agent(name="Researcher", instructions=RESEARCHER_BASE, model=RESEARCHER_MODEL, model_settings=SETTINGS)


@function_tool
async def research(ctx: RunContextWrapper[Research], task: str, specialty: str) -> str:
    """Send a researcher to answer one task with one data source. Returns its findings, each with evidence IDs.

    Args:
        task: One specific thing to find out, naming the corridor(s), years and business types.
        specialty: Which data the researcher uses: 'licenses', 'storefront' or 'demographics'.
    """
    spec = SPECIALTIES.get(specialty.strip().lower())
    if not spec:
        return f"Unknown specialty '{specialty}'. Use one of: {', '.join(SPECIALTIES)}."
    if ctx.context.researchers >= MAX_RESEARCHERS:
        return "No researchers left. Reply DONE."
    # What researcher.as_tool() does: run the researcher in a fresh context and return its answer.
    # Written out so each run gets a numbered copy, and its own column in the context panel.
    ctx.context.researchers += 1
    agent = researcher.clone(
        name=f"{spec['label']} Researcher {ctx.context.researchers}",
        instructions=f"{RESEARCHER_BASE}\n\n{spec['focus']}",
        tools=spec["tools"],
    )
    result = await Runner.run(agent, f"Task: {task}", context=ctx.context, hooks=ctx.context.hooks,
                              max_turns=RESEARCHER_MAX_TURNS)
    ctx.context.findings.append(f"## {agent.name}\nTask: {task}\n\n{result.final_output}")
    return str(result.final_output)


# --- The lead: clarifies, plans, fans out to researchers, and checks for gaps ---

CORRIDOR_LIST = "\n".join(f"- {k}: {c['name']} ({c['neighborhood']}, ZIPs {', '.join(c['zips'])})"
                          for k, c in sf.CORRIDORS.items())
SPECIALTY_LIST = "\n".join(f"- {k}: {s['about']}" for k, s in SPECIALTIES.items())

lead = Agent(
    name="Lead",
    instructions=f"""\
You lead a research team that studies how long-running small businesses on NYC commercial corridors close and
what replaces them. The data covers only these pilot corridors:
{CORRIDOR_LIST}

Your researchers, one per data source:
{SPECIALTY_LIST}

1. Clarify. The question needs a WHERE (one or more pilot corridors, or "all corridors") and a WHAT (a business
   type, or a measure such as closures, replacements, vacancy, rent). If it lacks either, or uses a vague idea
   like "gentrifying", "doing well" or "changing" without saying what to measure, reply with exactly one line:
   CLARIFY: <one short question, offering 2-3 concrete options>
   and nothing else. If the place isn't a pilot corridor, reply CLARIFY: saying it isn't covered and listing the
   corridors. If no WHEN is given, don't ask: use all available years and say so in the tasks.
2. Plan and fan out. Split the question into 2 to 4 independent tasks and call research once per task, all in
   the same turn, so the researchers work in parallel. Pick the specialty whose data fits each task. Name the
   corridor(s), years and business types in every task.
   For questions comparing corridors or relating rent to turnover, include a demographics task that uses
   compare_corridors.
3. Review. Read the findings. If part of the question is still unanswered and another task could answer it,
   call research again for just that part. Do this at most twice.
4. When the question is covered (or the data can't cover more), reply DONE and nothing else: a writer writes
   the report.""",
    model=MODEL,
    model_settings=SETTINGS,
    tools=[list_corridors, research],
)

# --- The writer: one call, after the research ends ---

writer = Agent(
    name="Writer",
    instructions="""\
Write a short report in markdown that answers the question, using only the findings you're given.

Structure:
1. A 2-3 sentence answer at the top (no heading).
2. A "##" section per part of the question.
3. One small markdown table where it helps (e.g. closed category -> replaced by, or corridor comparison).
4. "## What the data can't tell us": the gaps the researchers reported and the coverage limits that matter here.
5. "## Sources": one line per evidence ID you cited, copied from the evidence table.

Rules:
- Cite inline with the evidence IDs from the findings, e.g. [E3]. Every sentence with a number, name, date or
  address needs one. Only use IDs that appear in the evidence table. In tables, cite once per row (last column).
- Never give a reason why a business closed; the data shows when, not why.
- Rent, income and demographics are correlation only: never write that rent caused closures.
- Write for a small-business owner scouting a location: plain, specific sentences. Don't claim more
  significance than the findings do: no "profoundly", "pivotal", "paving the way", or "it is worth noting".""",
    model=MODEL,
    model_settings=SETTINGS,
)

CITATION = re.compile(r"\[(E\d+(?:\s*,\s*E\d+)*)\]")


def check_citations(report: str, valid: set[str]) -> list[str]:
    """Problems with the report's [E#] citations: IDs that don't exist, or no citations at all."""
    cited = {i.strip() for group in CITATION.findall(report) for i in group.split(",")}
    problems = [f"{i} is not in the evidence table" for i in sorted(cited - valid, key=lambda x: int(x[1:]))]
    if not cited and valid:
        problems.append("The report cites no evidence IDs at all")
    return problems


async def deep_research(question: str, corridor: str, hooks: PanelHooks) -> tuple[str, Research]:
    run = Research(hooks=hooks)

    # 1. Research: clarify, plan, fan out, check for gaps
    prompt = f"Question: {question}"
    if corridor:
        prompt += f"\nCorridor chosen by the user: {corridor}"
    lead_result = await Runner.run(lead, prompt, context=run, hooks=hooks, max_turns=LEAD_MAX_TURNS)
    lead_reply = str(lead_result.final_output).strip()
    if lead_reply.upper().startswith("CLARIFY:") or not run.findings:
        return lead_reply.split(":", 1)[1].strip() if ":" in lead_reply else lead_reply, run

    # 2. Write: one call, outside the loop. The Writer sees only the question, findings and evidence table.
    findings = "\n\n---\n\n".join(run.findings)
    writer_input = (f"Question: {question}\n\nFindings:\n\n{findings}\n\n"
                    f"Evidence table (cite only these IDs):\n{run.evidence.table()}")
    report = str((await Runner.run(writer, writer_input, context=run, hooks=hooks)).final_output)

    # 3. Citation check: send the draft back once if it cites evidence that doesn't exist.
    problems = check_citations(report, run.evidence.ids())
    if problems:
        hooks.activity("Writer", "citation check failed: " + "; ".join(problems))
        fix = (f"{writer_input}\n\nYour draft:\n\n{report}\n\nFix these citation problems and return the full "
               f"corrected report:\n- " + "\n- ".join(problems))
        report = str((await Runner.run(writer, fix, context=run, hooks=hooks)).final_output)
    return report, run


# --- FastAPI App ---

HERE = Path(__file__).parent

# run_id -> every agent's context, in order. In-memory, single process. The context panel reads it.
sessions: dict[str, AgentContexts] = {}

app = FastAPI()
add_context_panel(app, sessions)


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/corridors")
def corridors():
    return [{"key": k, "name": c["name"], "neighborhood": c["neighborhood"]} for k, c in sf.CORRIDORS.items()]


@app.get("/run")
async def run(question: str, corridor: str = ""):
    """Research one question and stream what happens as Server-Sent Events."""
    run_id = str(uuid.uuid4())
    sessions[run_id] = AgentContexts()
    queue: asyncio.Queue = asyncio.Queue()
    hooks = ResearchHooks(sessions[run_id], queue.put_nowait)

    async def work():
        try:
            report, state = await deep_research(question, corridor, hooks)
            queue.put_nowait({"type": "done", "markdown": report, "evidence": state.evidence.items,
                              "tool_calls": state.tool_calls})
        except Exception as e:
            # Auth, quota, a model that won't stop: show it on the page, not as a dropped stream.
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'run_id': run_id})}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("done", "error"):
                    break
        finally:
            task.cancel()  # the page closed the stream: stop spending tokens

    return StreamingResponse(events(), media_type="text/event-stream")


async def ask(question: str, corridor: str = "") -> None:
    """Command-line run: uv run app.py --ask "question" [--corridor myrtle_ave]"""
    hooks = ResearchHooks(AgentContexts(), lambda e: print(f"  · {e['agent']}: {e['text']}") if e["type"] == "activity" else None)
    report, state = await deep_research(question, corridor, hooks)
    print("\n" + "=" * 80 + "\n" + report + "\n" + "=" * 80)
    print(f"{len(state.tool_calls)} tool calls by {sorted({c['agent'] for c in state.tool_calls})}; "
          f"{len(state.evidence.items)} evidence items; citation problems: {check_citations(report, state.evidence.ids()) or 'none'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ask", help="run one question in the terminal instead of starting the server")
    parser.add_argument("--corridor", default="")
    args = parser.parse_args()
    if args.ask:
        asyncio.run(ask(args.ask, args.corridor))
    else:
        uvicorn.run(app, host="127.0.0.1", port=8000)
