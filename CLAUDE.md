# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Status

Built and deployed. The NYC storefront agent described below replaces the course starter `wikipedia-deep-research`: `app.py` (agents, `/run`, `/chat`) at the root; the `storefront/` package (`storefronts.py` DuckDB queries, `web.py` press search, `addresses.py` address normalization shared with the build, and the starter's unchanged `context_panel.py`/`context-panel.js`); `static/index.html` (bodega front end); and offline tools in `scripts/` (`validate_data.py`, `build_data.py`, `run_samples.py`). Scripts find the repo root via `Path(__file__).resolve().parent.parent` and add it to `sys.path`. Nothing in `scripts/` is imported by the server. The starter's `wikipedia.py` has been removed. "Planned architecture" below describes what was built.

## Commands

- Setup: create `.env` next to `app.py` (no `.env.example` is committed; only `CENSUS_API_KEY` is needed, and only for rebuilding the data), then `gcloud auth application-default login` for Vertex AI.
- Run locally: `uv run app.py`, then open http://localhost:8000.
- Run one question in the terminal (prints the activity stream, the report, and a citation check): `uv run app.py --ask "question" [--corridor myrtle_ave]`.
- Smoke-test the data tools without a model: `uv run python -c "from storefront import storefronts as s; print(s.find_closures('myrtle', since_year=2015))"`.
- Data coverage check: `uv run scripts/validate_data.py` (`--refresh` to re-download). It caches raw Socrata pulls in `data/raw/<source>__<corridor>.json` and writes `data/VALIDATION.md`. Source field mappings live in its `SOURCES` dict; `scripts/build_data.py` imports them. ACS needs `CENSUS_API_KEY` in `.env` (the Census API now rejects keyless calls with a 302).
- Build the database: `uv run scripts/build_data.py` (`--refresh` re-downloads first). It writes `data/storefronts.duckdb` (committed, ~3 MB) and `data/BUILD_REPORT.md` (match rates, per-corridor counts). Inspect it with `uv run python -c "import duckdb; print(duckdb.connect('data/storefronts.duckdb', read_only=True).sql('...'))"`.
- Regenerate the README's sample answers (baseline vs agent, with traces) into `docs/samples/`: `uv run scripts/run_samples.py [1 2 3]`.
- Deploy: push to `main`. A Cloud Build trigger (region northamerica-northeast1, buildpacks, entrypoint `uvicorn app:app --host 0.0.0.0 --port $PORT`) updates the Cloud Run service `storefront-succession-git` in europe-west1. Service settings such as timeout, memory and access are set on the service, not in the repo.
- Add a dependency: `uv add <pkg>` (keeps `uv.lock`, which must be committed, in sync).
- No tests or linter are configured yet.

## How the starter works (`app.py`)

- Models: `LitellmModel` on Vertex Gemini. `MODEL` (`vertex_ai/gemini-3.5-flash`) is used for the Lead and Writer, `RESEARCHER_MODEL` (`gemini-3.5-flash-lite`) for Researchers. Shared `SETTINGS` set a 90 s timeout and 2 retries. Tracing is disabled because it would otherwise upload to OpenAI.
- `Research` dataclass: the per-run state passed as the run `context`, holding the hooks, a researcher counter, and the `findings` list. The model never sees it.
- `research` tool: written out by hand instead of using `researcher.as_tool()`, so that each call does `researcher.clone(name=f"Researcher {n}")` and `Runner.run(...)` in a fresh context with the shared hooks. That gives each Researcher its own column in the context panel. `MAX_RESEARCHERS` is enforced here, in code.
- `deep_research()`: runs the Lead (which fans out by calling `research` several times in one turn and replies `DONE`), then makes a single Writer call outside the loop that sees only the question and the joined findings.
- `/run` (GET, SSE): emits events `start` (with `run_id`), `context`, `activity`, `done` (with `markdown`), and `error`. Cancelling the task when the client disconnects stops token spend. `static/index.html` consumes these through `EventSource`.
- Context panel: `add_context_panel(app, sessions)` adds `/context` and `/context-panel.js`. `sessions[run_id]` is an `AgentContexts`. `PanelHooks` (passed as `hooks=` to every `Runner.run`) records each agent's messages and emits the `context`/`activity` events. Keep `storefront/context_panel.py` and `storefront/context-panel.js` unchanged (and in the same folder: the .py serves the .js from its own directory). To add custom events, subclass `PanelHooks`; it is also the natural place to collect `tool_calls` with an `agent` field for `/chat`.
- Sessions are in memory in a single process, so Cloud Run runs with max instances 1 (otherwise a `/chat` follow-up to a `CLARIFY` question can land on an instance that doesn't have the session).

## What this is

"What Was Here Before": a deep-research agent that traces how long-running small businesses on NYC commercial corridors (laundromats, bodegas, delis, family restaurants) close and what replaces them (cafés, bars, chains, vacancy), and how that turnover relates to rent and demographic change. Target user: someone scouting a location on an NYC corridor.

It is built by extending the starter (same stack, same Lead → parallel Researchers → Writer pattern), swapping Wikipedia for NYC storefront data. The Lead/`research`/Writer structure, `Research`, `/run`, and the panel stay. What changes: the tools (the starter's `wikipedia.py` → `storefronts.py` + `web.py`), the prompts, the inputs (a question plus an optional corridor picker instead of a start URL), and `index.html`.

## Hard requirements (from the assignment; they're graded)

- Repo root must contain `app.py`, `pyproject.toml`, `uv.lock`, `README.md`, and `submission.json` with `deploy_url`, `authors` (Columbia UNIs), and `"path": "research"`.
- At least 3 agent shapes. The Lead fans out to several Researchers **in one turn** (parallel, fresh contexts), goes back for gaps up to a turn cap, and asks for clarification on vague questions. Output must be accurately cited.
- `/chat` keeps the Project 1 response shape. Every tool call, including sub-agents' calls, goes in `tool_calls` with an `agent` field (`lead`, `licenses_researcher`, …, `writer`).
- At least one data source per team member.
- The front end must differ from the starter and say what the agent explores, who it's for, and how to use it.
- Deployed to Cloud Run via continuous deploy from GitHub. Graders wait up to 5 min per answer, so raise the request timeout to about 10 min and cap `max_turns`.
- The README needs: data provenance, 3 sample queries with expected answers and their source tables, and for each query the no-tools/no-system-prompt model answer next to the agent's answer.

## Planned architecture

**Offline (`scripts/build_data.py`)**: download the sources → filter to the pilot ZIPs → normalize addresses to `HOUSE_NUMBER + STANDARDIZED_STREET + ZIP` (use BBL where available; handle aliases such as Frederick Douglass Blvd = 8th Ave) → assign a corridor from `data/corridors.yaml` → classify into about 15 categories from `data/categories.yaml` → write `data/storefronts.duckdb` with the tables `businesses`, `storefront_timeline`, `closures`, `vacancy`, `acs`. Validation output (row counts, date ranges, match rates) goes to `data/VALIDATION.md`. Log every download in `data/SOURCES.md`. Never edit `data/raw/` (gitignored).

**Data access**: `storefront/storefronts.py` holds DuckDB query functions (replacing the starter's `wikipedia.py`) and `storefront/web.py` handles web search/fetch. `app.py` wraps them as `@function_tool`. **Every tool returns JSON with `evidence_id`, `source`, `params`, `rows` (capped), and `note`**, catches its own errors, and returns `rows: []` plus a note when nothing matches. The citation system depends on this envelope.

**Agents (`app.py`)**:
- **Lead** (tools: `list_corridors`, `research`). If the question lacks a where, when, or what, it replies `CLARIFY: <question>` and the app returns that without running research. Otherwise it calls `research(task, specialty)` 2–4 times in one turn, follows up on gaps at most twice, and ends with `DONE`.
- **`research` tool**: enforces `MAX_RESEARCHERS`, clones the Researcher as `"{Specialty} Researcher {n}"` with that specialty's tools, runs `Runner.run(..., max_turns=10)` in a fresh context, and appends to `ctx.context.findings`.
- **Researcher**: one shape with four specialties, each tied to a data source:
  - `licenses`: DCWP licenses, NYS retail food stores, SLA
  - `storefront`: the NYC Storefront Registry
  - `demographics`: ACS rent/income/population
  - `press`: live web

  It returns at most 8 bullets, each ending in `[E#]`, plus a final "not answered" bullet.
- **Writer**: no tools, one call. It gets the question, the findings, and the evidence table. It writes markdown with: a short answer, a section per part of the question, a summary table, **"What the data can't tell us"**, and **Sources**.
- **Citation check**: code verifies that every `[E#]` in the report exists. If any don't, it sends the draft back to the Writer once with the problems listed.

## Verified data facts (from validation, 2026-11-04)

- Dataset IDs:
  - DCWP current `w7w3-xahh` and historical `m4ph-grrm`. Historical is the legacy system, with creation dates up to 2013, and it is the only source with LAUNDRY and SIDEWALK CAFE. It also has junk dates such as 1900 and 2223, so clamp them.
  - NYS food `9a8c-vfzj` (current licensees only, no dates).
  - SLA active `9s3h-dpkz` and inactive `6dg3-2z7i`, which use different field names.
  - The storefront registry is **one** stacked dataset, `92iy-9c3n`. Its `reporting_year` takes values from "2019 and 2020" through "2025". Its BBL identifies a building, not a storefront.
- Use tobacco/cigarette retail dealer licenses as the stand-in for bodegas and delis.
- SLA "original issue" dates are only about the current license cycle, so they can't show how long a business has been open.
- Single license records undercount long-running businesses because they get reissued. `scripts/build_data.py` chains records by address key plus name.
- Street spellings vary widely (e.g. "FREDRICK DOUGLASS BL", "*8 Avenue"). `data/corridors.yaml` has `like_terms` (a server-side prefilter) and `street_patterns` (regexes that decide). Recheck the street-match % in VALIDATION.md after editing either.

## Agent wiring (`app.py`, `storefront/storefronts.py`)

- `storefronts.py` holds pure query functions that return `{tool, source, params, rows, total_rows, note}`. In `app.py`, each `@function_tool` wrapper passes the result through `_cite()`, which stamps it with `E#` from the run's `EvidenceLog`. `COVERAGE` gives per-category caveats (e.g. laundries are invisible after 2013), and these are injected into `note`.
- Specialties live in `SPECIALTIES` (licenses, storefront, demographics, press). Press uses `web.py`: Gemini with Google Search grounding (thinking off, ~4 s/search, retries on 429). It keeps only grounding-supported sentences per source and resolves the vertexaisearch redirect URLs. `fetch_page` refuses non-public hosts, including the Cloud Run metadata server, and checks every redirect hop. `research(task, specialty)` clones the one `researcher` with that specialty's tools and focus.
- Lead returns `CLARIFY: ...` (the app returns the question without running research) or `DONE`. The Writer gets the findings plus `EvidenceLog.table()`. `check_citations` sends the draft back once if it contains unknown `[E#]` IDs.
- Latency guards (Cloud Run timeout is 300 s): `TOTAL_BUDGET_S` 270, `RESEARCHER_TIMEOUT_S` 110, `RESEARCH_PHASE_TIMEOUT_S` 170. The Writer gets the remaining time. The citation-fix rewrite is skipped below `WRITER_MIN_S`. If the Writer times out, the raw findings and sources are returned. Typical runs take 60–120 s.
- `/chat` keeps the Project 1 shape (`response`, `session_id`, `tool_calls[{name,args,result,agent}]`) plus per-session history, so a reply to a `CLARIFY` question is understood in context.
- `ResearchHooks.on_tool_end` records every tool call as `{agent, name, arguments, evidence_id, output}` in `Research.tool_calls`, for `/chat`.

## Build design (`scripts/build_data.py`, `storefront/addresses.py`)

- `address_key = "<corridor>|<house number>"`. The street is implied, because every row was already matched to one corridor street. Queens numbers are normalized to `NN-NN`. Registry rows take their number from `property_street_address_or` (the storefront), not from `property_number` (the building).
- Merging: within one address_key, records are union-found into a business when their names are equal, one contains the other, or they share ≥50% of their distinctive words (generic words such as DELI/CORP/LLC are ignored).
- Status: `open` (any record active), `closed`, `lapsed` (expired < 12 months ago with no definite-close status), or `unknown` (only legacy records still current at the Oct 2013 export). Only `closed` businesses go in `closures`.
- `start_reliable` is true only when the start date comes from DCWP. SLA dates restart with each license.
- Replacement: `replacement_basis` is `license` (the next business started within 12 months), `registry` (vacant or a registry activity), `license_after_gap`, or `none`.
- The registry label "X and Y" maps to year X. `vacancy.partial_year` flags the year still being filed.

## Domain rules the agents must follow

- Never invent businesses, dates, numbers, or closure reasons. Give a *why* for a closure only when a Press finding with a URL supports it.
- Rent/demographic relationships are correlation only.
- Known data gaps to surface, never paper over:
  - DCWP covers only city-licensed industries; bodegas and barbershops aren't in it.
  - The storefront registry starts around 2020, so there's no earlier vacancy data.
  - COVID-era license extensions aren't reflected in expiration dates.
  - NYS food/SLA data may contain only active licensees, which limits closure history.
- Definitions:
  - **Long-running**: active ≥ 10 years.
  - **Closed**: license inactive or expired and not renewed within 12 months, or gone from the next registry year.
  - **Replacement**: the next business at the same address, or "vacant" if none within 12 months.
  - **Turnover rate**: closures ÷ storefronts per year.
- Writing style (kept from the starter): plain language, no "pivotal" or "it is worth noting".

Pilot corridors (swap out any with thin coverage):

| Corridor | Neighborhood |
|---|---|
| Frederick Douglass Blvd | Central Harlem |
| Myrtle Ave | Bushwick / Ridgewood (incl. 11385) |
| 5th Ave | Sunset Park |
| Roosevelt Ave | Jackson Heights |
| Nostrand Ave | Crown Heights / Flatbush |
