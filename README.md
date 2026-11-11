# What Was Here Before

**Thinking of opening on an NYC corridor? See what happened to the businesses before you.**

A deep-research agent (Project 2, Deep Research path) that traces how long-running small businesses on NYC commercial corridors (laundromats, bodegas, delis, family restaurants) close, what replaces them (cafés, bars, chains, or vacancy), and how that turnover lines up with rent and demographic change. A Lead agent plans the question and sends Researchers out in parallel, one per data source. A Writer then turns their findings into a cited report.

**Who it's for:** small-business owners scouting a location, plus residents, Community Board members and local reporters watching a block change.

**Live app:** https://storefront-succession-git-895199835501.europe-west1.run.app (Cloud Run, continuous deploy from `main`). Built on the course `wikipedia-deep-research` starter.

## Pilot corridors

| Corridor | Neighborhood | ZIPs |
|---|---|---|
| Frederick Douglass Blvd (= 8th Ave) | Central Harlem | 10026, 10027, 10030, 10039 |
| Myrtle Ave | Bushwick / Ridgewood | 11206, 11221, 11237, 11385 |
| 5th Ave | Sunset Park | 11220, 11232 |
| Roosevelt Ave | Jackson Heights | 11372, 11373 |
| Nostrand Ave | Crown Heights / Flatbush | 11216, 11225, 11226 |

A record belongs to a corridor when its ZIP is in the list and its street matches the corridor's patterns. Both are defined in [`data/corridors.yaml`](data/corridors.yaml), which handles spelling variants such as "FREDRICK DOUGLASS BL" and "8 AVENUE". Myrtle Ave includes ZIP 11385 because the Ridgewood end is the busiest stretch. Bushwick alone had only 5 long-running closures in the data; with Ridgewood it has 33.

## Data sources

All sources are public. The NYC and NYS datasets need no account. Each one feeds a different Researcher.

| # | Source | Publisher / dataset ID | What one row is | What we use it for | Researcher |
|---|---|---|---|---|---|
| 1 | [Issued Licenses](https://data.cityofnewyork.us/d/w7w3-xahh) | NYC Dept. of Consumer & Worker Protection (DCWP), `w7w3-xahh` | A city business license (current system, about 2014 onward) | Opening and closing dates and status for licensed storefronts: tobacco retailers (mostly bodegas and delis), electronics stores, secondhand dealers | Licenses |
| 2 | [Historical Licenses](https://data.cityofnewyork.us/d/m4ph-grrm) | NYC DCWP, `m4ph-grrm` | A license from the legacy system (creation dates up to 2013) | The pre-2014 baseline, including **laundries**, **sidewalk cafés** and **cigarette retail dealers** | Licenses |
| 3 | [Retail Food Stores](https://data.ny.gov/d/9a8c-vfzj) | NYS Dept. of Agriculture & Markets, `9a8c-vfzj` | A currently licensed food store, with square footage | Which bodegas, delis and groceries are open **now** | Licenses |
| 4 | [Liquor Authority: Active Licenses](https://data.ny.gov/d/9s3h-dpkz) and [Inactive Licenses](https://data.ny.gov/d/6dg3-2z7i) | NYS State Liquor Authority (SLA), `9s3h-dpkz`, `6dg3-2z7i` | A liquor license (bar, restaurant, grocery beer, liquor store) | When bars and restaurants arrived and when they lapsed | Licenses |
| 5 | [Storefronts Reported Vacant or Not](https://data.cityofnewyork.us/d/92iy-9c3n) | NYC Dept. of Finance (Local Law 157 storefront registry), `92iy-9c3n` | One ground-floor storefront filing per reporting year | Storefront vacancy on 12/31 and the occupant's primary business activity, from the 2019/20 through 2025 reporting years | Storefront |
| 6 | [American Community Survey 5-year](https://www.census.gov/data/developers/data-sets/acs-5year.html) | U.S. Census Bureau, tables `B25064` (median gross rent), `B19013` (median household income), `B01003` (population) | A ZIP Code Tabulation Area (ZCTA) | Rent, income and population for 2011–2015 vs. the latest 5-year period | Demographics |
| 7 | Reports and local press (Patch, THE CITY, Eater NY, neighborhood blogs, city reports) | Live **Google Search via Gemini grounding** on Vertex AI, at question time ([`storefront/web.py`](storefront/web.py)) | A web source and the sentences it supports | The only source that can say **why** a business closed. Each claim is attributed to its outlet and URL. | Press |

### How to load the data

The NYC and NYS datasets are Socrata open-data APIs, so you can query any of them in a browser. For example, all historical Myrtle Ave licenses in Bushwick and Ridgewood:

```
https://data.cityofnewyork.us/resource/m4ph-grrm.json?$where=address_zip in('11206','11221','11237','11385') AND upper(address_street_name) like '%MYRTLE%'
```

To pull everything for the pilot corridors and regenerate the coverage report:

```bash
uv sync
uv run scripts/validate_data.py            # downloads to data/raw/ (gitignored, cached) and writes data/VALIDATION.md
uv run scripts/validate_data.py --refresh  # re-download instead of using the cache
```

ACS requires a free Census API key ([sign up here](https://api.census.gov/data/key_signup.html)). Put it in `.env` as `CENSUS_API_KEY=...`. Without the key, the script skips the ACS step and says so in the report. A `SOCRATA_APP_TOKEN` is optional and only raises rate limits.

The data was last downloaded on 2026-11-04.

### Building the database

The agent doesn't query the open-data APIs live. An offline step, [`scripts/build_data.py`](scripts/build_data.py), turns the cached pulls into `data/storefronts.duckdb` (about 3 MB, committed to the repo):

```bash
uv run scripts/build_data.py            # uses data/raw/ (fetches anything missing)
uv run scripts/build_data.py --refresh  # re-download all sources first
```

The build:
1. Gives every record an **address key** (`corridor|house number`) so the same storefront matches across sources. For example, Queens `7601`, `76 01` and `76-01` all become `76-01`.
2. Sorts every record into one of about 18 categories using [`data/categories.yaml`](data/categories.yaml). It checks the business name first, then the source's own label (tobacco license → bodega/deli, `LAUNDRY` → laundromat, and so on).
3. Merges records at the same address that name the same business into **one business**. For example, a deli's tobacco license and its beer license become one business, and a laundromat's 1995 license joins its 2014 renewal.
4. Finds each closed business's **replacement**: the next business at that address, or "vacant" according to the storefront registry.

| Table | One row per |
|---|---|
| `records` | source record (normalized), for citations |
| `businesses` | merged business: name, category, address, start/end, status, sources |
| `closures` | closed business: years open, `long_running`, what replaced it and how we know |
| `storefront_timeline` | occupant of an address, in order: businesses plus registry spells ("Vacant 2021–2022") |
| `registry`, `vacancy` | registry filing; corridor × year vacancy rate |
| `acs` | corridor × ZCTA: early vs. latest rent, income, population |

Match rates (from the last build): 99.7–100% of on-corridor records in every source got an address key. 4,639 license records merged into 2,895 businesses, with 172 long-running closures across the five corridors. Of the registry's storefront addresses, 48% also appear in a license dataset; the rest are storefront types the license datasets don't cover. Full numbers are in [`data/BUILD_REPORT.md`](data/BUILD_REPORT.md).

### What the data can't tell us

- **Not every business is licensed by the city or state.** DCWP licenses only certain industries. Barbershops, salons and most general retail aren't in any license dataset. Bodegas show up through tobacco, food-store and beer licenses.
- **The NYS food store data has no history.** It lists only stores licensed today.
- **SLA issue dates show the current license, not when the business opened.** Active licenses on these corridors show "original issue" dates from 2023–2026, and inactive ones go back only to about 2012. SLA data can show that a bar or restaurant closed, but not how long it had been open.
- **Vacancy data starts in the 2019/20 reporting year.** Before that, vacancy can only be inferred from licenses lapsing. The registry also describes each storefront by a broad activity ("RETAIL", "FOOD SERVICES"), not by its business name.
- **The legacy DCWP export stops in October 2013.** If a business's last record is a legacy license that was still current then (most laundromats, since the current system no longer lists laundries), its status is **unknown**, not closed.
- **Address keys merge everything at one house number.** Two shops in the same building can look like one storefront.
- **COVID-era license extensions** aren't reflected in expiration dates.
- **Rent and demographics are correlation only.** ZCTA-level ACS numbers can't show what caused a particular closure.

## Definitions

- **Long-running business:** active at least 10 years.
- **Closed:** every license for the business is inactive. A business whose license expired less than 12 months ago is **lapsed**, not closed, because it may still renew. Expiration dates later than the build date are capped at the build date.
- **Unknown:** the business was last seen as current in the 2013 legacy export.
- **Replacement:** the next business at the same address, or "vacant" if there is none within 12 months.
- **Turnover rate:** closures ÷ storefronts per year.

## Sample queries

Paste any of these into the web app, or send them to `/chat`:

```bash
curl -s -X POST https://storefront-succession-git-895199835501.europe-west1.run.app/chat \
  -H 'Content-Type: application/json' -d '{"message": "<question>"}' | jq .response
```

Each query was run three ways:
- **Expected:** computed directly from `data/storefronts.duckdb` with the SQL shown. The tables trace back to the source datasets listed under [Data sources](#data-sources).
- **Baseline:** the same model (`gemini-3.5-flash`) with no tools and no system prompt.
- **Agent:** our full Lead → Researchers → Writer pipeline.

The baseline and agent columns quote the saved answers directly. The full text of both answers and every tool call (with its `agent`) are in [`docs/samples/`](docs/samples/). To regenerate them, run `uv run scripts/run_samples.py`. Agent wording changes from run to run; the numbers should not.

### 1. On Myrtle Ave, what kinds of businesses that ran 10+ years closed since 2015, and what replaced them?

**Expected answer.** About 30 long-running closures. By category: bodegas/delis 15, pharmacies 4, bars 2, phone/electronics 2, other chains 2, and one each of grocery, restaurant, services and auto. Most (17) have no recorded successor. The known successors are mixed: services 3, retail 2, restaurant 2, and one each of bar, bodega, pharmacy and vacant. Examples: Eckerd #5537 (60-36 Myrtle) → Walgreens; Vaqueros (1541 Myrtle) → Aby's Bar; Aryan Grocery (79-39 Myrtle) → The Corner Deli Grill.

**Comes from:** table `closures` (built from DCWP `w7w3-xahh`/`m4ph-grrm`, NYS food `9a8c-vfzj`, SLA `9s3h-dpkz`/`6dg3-2z7i`, registry `92iy-9c3n`). The agent reaches it through the `find_closures` and `replacements` tools.

```sql
SELECT category, replaced_by_category, count(*) FROM closures
WHERE corridor = 'myrtle_ave' AND long_running AND closed_year >= 2015 GROUP BY ALL ORDER BY 3 DESC;
```

| | Baseline (no tools, no system prompt) | Agent (84 s, 22 tool calls) |
|---|---|---|
| **Answer** | "The transformation of Myrtle Avenue in Brooklyn (specifically the stretch running through Fort Greene, Clinton Hill, and Bedford-Stuyvesant) since 2015 is a classic study in urban gentrification." | "Since 2015, 29 long-running businesses operating for 10 or more years closed on Myrtle Avenue, including several bodegas, pharmacies, electronics stores, and bars [E3]." |
| **Specifics** | Associated Supermarket (490 Myrtle), Five Spot Soul Food, Maggie Brown, White Castle → "CityMD". All are on the Fort Greene end, outside this corridor, and none are in the data. | "Goldstar Chen Inc at 68-03 Myrtle Ave closed in 2022 after 22.3 years [E18]", "Care Express Pharmacy Inc at 64-16 Myrtle Ave closed in 2025 after 24.2 years [E9]", plus a replacement table. |
| **Sources** | None | Every sentence cites an evidence ID that maps to a tool call |
| **Gaps admitted** | None | Says many storefronts have no recorded successor |
| **Matches expected?** | No: wrong stretch of Myrtle, and the businesses are unverifiable | Yes: 29 vs. ~30, same category mix |

### 2. Which pilot corridors lost the most laundromats and delis between 2013 and today?

This one tests whether the agent admits a gap in the data instead of filling it.

**Expected answer.** Bodega/deli closures since 2013: Nostrand 97, Myrtle 60, 5th Ave 49, Frederick Douglass 33, Roosevelt 27. **Laundromats after 2013 can't be counted.** Laundries appear only in the DCWP legacy export, which ends in October 2013, so 81 laundromats last seen there have status `unknown`. For 2005–2013 the counts are Nostrand 30, Myrtle 24, Frederick Douglass 13, 5th Ave 10, Roosevelt 7.

**Comes from:** tables `closures` and `businesses`. Delis come from DCWP tobacco licenses and NYS food stores; laundromats come from DCWP historical `m4ph-grrm` only. The agent reaches them through the `category_trend` and `find_closures` tools.

```sql
SELECT corridor, count(*) FROM closures WHERE category = 'bodega_deli' AND closed_year >= 2013 GROUP BY 1 ORDER BY 2 DESC;
SELECT corridor, count(*) FROM businesses WHERE category = 'laundromat_drycleaner' AND status = 'unknown' GROUP BY 1;
```

| | Baseline (no tools, no system prompt) | Agent (116 s, 33 tool calls) |
|---|---|---|
| **Answer** | "This question refers to the New York City Department of City Planning (DCP) Storefront Survey ... across 24 pilot commercial corridors." That survey and its corridors don't exist. | "Nostrand Ave lost the most bodega and deli businesses with 97 recorded closures, followed by Myrtle Ave with 60 closures [E11, E15]." |
| **Specifics** | Names Grand Street, Fulton Street, Utica Ave and Church Ave, none of which are our pilot corridors. Gives no numbers, plus pandas code for a dataset that doesn't exist. | All five corridors' deli counts, plus examples: "Myrtle Market Place at 1218 Myrtle Ave closed in 2025 and was replaced by EJ Organic Market [E25]." |
| **Sources** | None | Evidence IDs; Press findings are attributed to their outlets |
| **Gaps admitted** | None: it says the laundromat losses are known | "The dataset cannot determine which pilot corridor lost the most laundromats during this period because ... legacy licensing data for laundromats ends in October 2013 [E5, E6]." |
| **Matches expected?** | No | Yes: exact deli counts, and it refuses to count laundromats it can't see |

### 3. Do corridors with the biggest rent increases have higher turnover of long-running businesses?

**Expected answer.**

| Corridor | Rent change (ACS 2011–15 → 2020–24) | Long-running closure rate since 2014 | Annual turnover |
|---|---|---|---|
| Myrtle Ave | +71.3% | 0.274 | 0.066 |
| Nostrand Ave | +62.9% | 0.292 | 0.108 |
| 5th Ave | +50.6% | 0.190 | 0.062 |
| Frederick Douglass Blvd | +49.4% | 0.265 | 0.083 |
| Roosevelt Ave | +39.7% | 0.167 | 0.063 |

The Spearman rank correlation between rent change and long-running closure rate is **0.8**; between rent change and overall turnover it is **0.3**. That suggests a pattern, but with n = 5 it isn't conclusive, and it is correlation only.

**Comes from:** tables `acs` (Census ACS `B25064` via api.census.gov), `closures` and `businesses`. The agent gets the joined table and the Spearman row from the `compare_corridors` tool (`metric=rent_pct_change`).

| | Baseline (no tools, no system prompt) | Agent (123 s, 33 tool calls) |
|---|---|---|
| **Answer** | "Based on urban planning research, economic data, and case studies from major global cities, the short answer is **yes**." | "Corridors with the highest rent increases show a moderate positive correlation with higher closure rates among long-running businesses, though this relationship does not apply as strongly to overall annual business turnover [E3]." |
| **Specifics** | Lease shocks, triple-net leases, "high-rent blight", and studies of San Francisco and London. Nothing about these corridors. | The table above with the same numbers, "a Spearman rank correlation of 0.8 [E3]" and "much weaker at 0.3". Also sourced press reasons, e.g. "According to News 12 in January 2025, Bushwick Public House (1288 Myrtle Ave) closed ... because the landlord tripled the rent [E27]." |
| **Sources** | Names Pratt Center and ANHD research, but gives no links or numbers | Evidence IDs that map to `compare_corridors`, SQL and web results |
| **Gaps admitted** | None: states causation as fact | "Restricted to only five pilot corridors ... any relationships identified between rent and business turnover are correlation only [E3]." |
| **Matches expected?** | No data to compare | Yes: same table, same 0.8 / 0.3 |

### Edge cases

| Input | Behaviour |
|---|---|
| "Is Nostrand Ave gentrifying?" | Asks what to measure (rent and income, closures, or vacancy). A short reply in the same session continues the research. |
| "What about Astoria?" | Says Astoria isn't covered and offers the pilot corridors. |
| "Why did the Rite Aid on Frederick Douglass Blvd close?" | The Press Researcher finds Rite Aid's Chapter 11 bankruptcy and attributes it to named outlets. The Writer gives a reason only when a press source supports it. |

## Repo layout

```
app.py             the agents (Lead, Researchers, Writer) and the web server: /, /run, /chat
storefront/        code the app imports
  storefronts.py     DuckDB query functions behind the data tools
  web.py             press search (Gemini + Google Search grounding) and page fetch
  addresses.py       address normalization, shared with the offline build
  context_panel.py   the starter's context panel (+ context-panel.js)
static/index.html  the front end
scripts/           offline tools, not used by the server
  validate_data.py   download sources and write data/VALIDATION.md
  build_data.py      build data/storefronts.duckdb and data/BUILD_REPORT.md
  run_samples.py     regenerate docs/samples/
data/              corridor and category definitions, the built database, reports
docs/samples/      saved sample answers and traces
```

## Running the app

The app has four agent types: a **Lead** that plans, four kinds of **Researcher** (Licenses, Storefront, Demographics, Press) that run in parallel in fresh contexts, and a **Writer** that produces the cited report. Every tool result gets an evidence ID (`E1`, `E2`, ...). The report cites those IDs, and code checks that every ID it cites exists.

### Endpoints

- `GET /run?question=...&corridor=...`: Server-Sent Events stream used by the web page.
- `POST /chat` with `{"message": "...", "session_id": null, "corridor": null}`. Returns the Project 1 shape: `{"response", "session_id", "tool_calls": [{"name", "args", "result", "agent", ...}]}`. `agent` is `lead`, `licenses_researcher`, `storefront_researcher`, `demographics_researcher` or `press_researcher`. If the agent asks a clarifying question, send the answer with the same `session_id`.
- `POST /clear?session_id=...`: forget a chat session.

```bash
gcloud auth application-default login   # Vertex AI (Gemini)
uv run app.py                           # http://localhost:8000
```

### Front end

The page opens on an NYC bodega storefront, drawn in HTML/CSS: a brick façade, a back-lit DELI · GROCERY sign, a neon OPEN sign, marker posters and a bodega cat. Click (or press Enter on) the doors and they swing open as the view moves through the doorway. Inside is a chalkboard bodega menu: corridors are menu items with dotted price leaders, example questions are "Today's specials", researchers show up as order tickets, and citations look like price tags. "Skip the door", reduced-motion settings and `?q=` links skip the animation.

[`static/index.html`](static/index.html) shows:
- what the agent explores, who it's for, and how to use it
- corridor cards with live stats, which also work as the corridor picker
- example questions you can click
- an inline box for answering clarifying questions
- an instant, model-free address lookup that draws the "what was here before" chain
- one lane per agent while research runs
- the report rendered as markdown, with citation chips that show their source on hover
- timeline chains for every storefront the agents looked up
- the starter's context panel, behind a toggle

`/?q=...&corridor=...` links start a run directly.

## Deployment

Cloud Run service `storefront-succession-git` (europe-west1) is redeployed by a Cloud Build trigger on every push to `main`. The trigger builds with Google buildpacks, which read `uv.lock`, and starts `uvicorn app:app --host 0.0.0.0 --port $PORT`.

The database (`data/storefronts.duckdb`, ~3 MB) ships in the repo, so nothing is downloaded at startup.

Service settings, which persist across deploys:

| Setting | Value | Why |
|---|---|---|
| Request timeout | 300 s (5 min) | Same as the graders' wait. Runs take 1–2 min, and the app keeps itself under 270 s: researchers stop at 110 s, the research phase at 170 s, and the Writer gets the time that's left. |
| Memory | 1 GiB | |
| Session affinity | on | Run state is kept in memory |
| Max instances | 1 | Chat sessions live in one process's memory, so a `/chat` reply to a clarifying question must reach the same instance (session affinity only helps browsers, not `curl`) |
| Access | unauthenticated | |
