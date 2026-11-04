# What Was Here Before

**Thinking of opening on an NYC corridor? See what happened to the businesses before you.**

A deep-research agent (Project 2, Deep Research path) that traces how long-running small businesses on NYC commercial corridors (laundromats, bodegas, delis, family restaurants) close, what replaces them (cafés, bars, chains, or vacancy), and how that turnover lines up with rent and demographic change. A Lead agent plans the question and sends Researchers out in parallel, one per data source. A Writer then turns their findings into a cited report.

**Who it's for:** small-business owners scouting a location, plus residents, Community Board members and local reporters watching a block change.

> Status: data validation is done (see [`data/VALIDATION.md`](data/VALIDATION.md)). The agent, front end and deploy are in progress. The app code is still the course `wikipedia-deep-research` starter.

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
| 7 | Reports and local press (NYC DCP storefront reports, NYC Council data team, THE CITY, Eater NY, Patch, neighborhood blogs) | Live web search at question time | A web page | The only source that can say **why** a business closed | Press |

### How to load the data

The NYC and NYS datasets are Socrata open-data APIs, so you can query any of them in a browser. For example, all historical Myrtle Ave licenses in Bushwick and Ridgewood:

```
https://data.cityofnewyork.us/resource/m4ph-grrm.json?$where=address_zip in('11206','11221','11237','11385') AND upper(address_street_name) like '%MYRTLE%'
```

To pull everything for the pilot corridors and regenerate the coverage report:

```bash
uv sync
uv run validate_data.py            # downloads to data/raw/ (gitignored, cached) and writes data/VALIDATION.md
uv run validate_data.py --refresh  # re-download instead of using the cache
```

ACS requires a free Census API key ([sign up here](https://api.census.gov/data/key_signup.html)). Put it in `.env` as `CENSUS_API_KEY=...`. Without the key, the script skips the ACS step and says so in the report. A `SOCRATA_APP_TOKEN` is optional and only raises rate limits.

The data was last downloaded on 2026-11-04.

### What the data can't tell us

- **Not every business is licensed by the city or state.** DCWP licenses only certain industries. Barbershops, salons and most general retail aren't in any license dataset. Bodegas show up through tobacco, food-store and beer licenses.
- **The NYS food store data has no history.** It lists only stores licensed today.
- **SLA issue dates show the current license, not when the business opened.** Active licenses on these corridors show "original issue" dates from 2023–2026, and inactive ones go back only to about 2012. SLA data can show that a bar or restaurant closed, but not how long it had been open.
- **Vacancy data starts in the 2019/20 reporting year.** Before that, vacancy can only be inferred from licenses lapsing. The registry also describes each storefront by a broad activity ("RETAIL", "FOOD SERVICES"), not by its business name.
- **COVID-era license extensions** aren't reflected in expiration dates.
- **Rent and demographics are correlation only.** ZCTA-level ACS numbers can't show what caused a particular closure.

## Definitions

- **Long-running business:** active at least 10 years.
- **Closed:** license inactive, or expired and not renewed within 12 months; or the business is gone from the next registry year.
- **Replacement:** the next business at the same address, or "vacant" if there is none within 12 months.
- **Turnover rate:** closures ÷ storefronts per year.

## Running the app

```bash
gcloud auth application-default login   # Vertex AI (Gemini)
uv run app.py                           # http://localhost:8000
```
