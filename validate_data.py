"""Step 1: check that each data source actually covers the pilot corridors, before building anything.

    uv run validate_data.py            # download (cached in data/raw/), then write data/VALIDATION.md
    uv run validate_data.py --refresh  # re-download instead of using the cache

For each source x corridor it reports record counts, date ranges, how many records are inactive,
and a preview of "long-running closures" (open >= 10 years, now inactive). The storefront registry
gets vacancy by reporting year. ACS rent/income/population is fetched per ZIP if CENSUS_API_KEY is
set in .env (the Census API now requires a free key: https://api.census.gov/data/key_signup.html).

Raw downloads are saved untouched as JSON in data/raw/ and never edited.
"""

import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

HERE = Path(__file__).parent
DATA = HERE / "data"
RAW = DATA / "raw"
load_dotenv(HERE / ".env")

TODAY = date.today()
LONG_RUNNING_YEARS = 10

# One entry per source. Field names come from each dataset's Socrata schema.
SOURCES = {
    "dcwp_current": {
        "label": "NYC DCWP Issued Licenses (current system)",
        "domain": "data.cityofnewyork.us", "id": "w7w3-xahh",
        "zip": "address_zip", "street": "address_street_name", "building": "address_building",
        "name": ["dba_trade_name", "business_name"], "category": "business_category",
        "status": "license_status", "start": "license_creation_date", "end": "lic_expir_dd",
        "active_statuses": {"ACTIVE", "READY FOR RENEWAL"},
    },
    "dcwp_historical": {
        "label": "NYC DCWP Historical Licenses (legacy system)",
        "domain": "data.cityofnewyork.us", "id": "m4ph-grrm",
        "zip": "address_zip", "street": "address_street_name", "building": "address_building",
        "name": ["business_name2", "business_name"], "category": "business_code_description",
        "status": "license_status", "start": "license_creation_date", "end": "license_status_date",
        "active_statuses": {"CURRENT", "REINSTATED", "PENDING", "RECEIVED AT COUNTER",
                            "APPROVAL-NEW-LICENSE", "RENEWAL-APPROVAL", "LICENSEE ALERT", "APPLICANT ALERT"},
    },
    "nys_retail_food": {
        "label": "NYS Ag & Markets Retail Food Stores (current licensees only)",
        "domain": "data.ny.gov", "id": "9a8c-vfzj",
        "zip": "zip_code", "street": "street_name", "building": "street_number",
        "name": ["dba_name", "entity_name"], "category": "estab_type",
        "status": None, "start": None, "end": None, "all_active": True,
    },
    "sla_active": {
        "label": "NYS Liquor Authority - Current Active Licenses",
        "domain": "data.ny.gov", "id": "9s3h-dpkz",
        "zip": "zipcode", "street": "actualaddressofpremises", "building": None,
        "name": ["legalname"], "category": "description",
        "status": None, "start": "originalissuedate", "end": "expirationdate", "all_active": True,
    },
    "sla_inactive": {
        "label": "NYS Liquor Authority - Current Inactive Licenses",
        "domain": "data.ny.gov", "id": "6dg3-2z7i",
        "zip": "zip_code", "street": "actual_address_of_premises", "building": None,
        "name": ["legalname"], "category": "description",
        "status": None, "start": "original_issue_date", "end": "expiration_date", "all_active": False,
    },
    "storefront_registry": {
        "label": "NYC DOF Storefronts Reported Vacant or Not (Local Law 157 registry)",
        "domain": "data.cityofnewyork.us", "id": "92iy-9c3n",
        "zip": "zip_code", "street": "property_street", "building": "property_number",
        "name": [], "category": "primary_business_activity",
        "status": None, "start": None, "end": None, "registry": True,
    },
}

ACS_VARS = {"B25064_001E": "median_gross_rent", "B19013_001E": "median_household_income",
            "B01003_001E": "population"}
ACS_EARLY = 2015  # 2011-2015 5-year estimates
ACS_LATEST_CANDIDATES = [2024, 2023, 2022]  # first one that exists is used


# --- Download ---

def soql_get(client: httpx.Client, domain: str, dataset: str, where: str) -> list[dict]:
    """Every row matching `where`, paging through the Socrata API."""
    rows, offset, page = [], 0, 50_000
    headers = {"X-App-Token": os.environ["SOCRATA_APP_TOKEN"]} if os.environ.get("SOCRATA_APP_TOKEN") else {}
    while True:
        params = {"$where": where, "$limit": page, "$offset": offset, "$order": ":id"}
        r = client.get(f"https://{domain}/resource/{dataset}.json", params=params, headers=headers)
        r.raise_for_status()
        batch = r.json()
        rows += batch
        if len(batch) < page:
            return rows
        offset += page


def fetch(client: httpx.Client, key: str, src: dict, corridor_key: str, corridor: dict, refresh: bool) -> list[dict]:
    path = RAW / f"{key}__{corridor_key}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text())
    zips = ",".join(f"'{z}'" for z in corridor["zips"])
    likes = " OR ".join(f"upper({src['street']}) like '%{t}%'" for t in corridor["like_terms"])
    rows = soql_get(client, src["domain"], src["id"], f"{src['zip']} in ({zips}) AND ({likes})")
    RAW.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows))
    return rows


# --- Cleaning helpers ---

def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").upper()).strip()


def on_corridor(row: dict, src: dict, patterns: list[re.Pattern]) -> bool:
    street = norm(row.get(src["street"]))
    return any(p.search(street) for p in patterns)


def parse_date(s) -> date | None:
    """Socrata dates look like 2018-05-30T00:00:00.000. Drop junk (1900, 2223, ...)."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s)[:10]).date()
    except ValueError:
        return None
    return d if date(1950, 1, 1) <= d <= date(TODAY.year + 5, 12, 31) else None


def is_inactive(row: dict, src: dict) -> bool:
    if "all_active" in src:
        return not src["all_active"]
    return norm(row.get(src["status"])) not in src["active_statuses"]


def first(row: dict, fields: list[str]) -> str:
    return next((str(row[f]).strip() for f in fields if row.get(f)), "")


# --- Per-source summaries ---

def summarize_licenses(rows: list[dict], src: dict) -> dict:
    starts = [d for r in rows if (d := parse_date(r.get(src["start"]))) ] if src["start"] else []
    inactive = [r for r in rows if is_inactive(r, src)]
    closures = []
    for r in inactive:
        s, e = parse_date(r.get(src["start"])) if src["start"] else None, parse_date(r.get(src["end"])) if src["end"] else None
        if s and e and e > s and (e - s).days / 365.25 >= LONG_RUNNING_YEARS:
            closures.append((first(r, src["name"]), norm(r.get(src["category"])), s.year, e.year,
                             f"{norm(r.get(src['building']) if src['building'] else '')} {norm(r.get(src['street']))}".strip()))
    closures.sort(key=lambda c: c[3], reverse=True)
    return {
        "n": len(rows),
        "start_range": (min(starts).year, max(starts).year) if starts else None,
        "inactive": len(inactive),
        "long_running_closures": closures,
        "categories": Counter(norm(r.get(src["category"])) or "(blank)" for r in rows).most_common(6),
    }


def summarize_registry(rows: list[dict], src: dict) -> dict:
    by_year: dict[str, list[dict]] = {}
    for r in rows:
        by_year.setdefault(r.get("reporting_year", "?"), []).append(r)
    years = []
    for y in sorted(by_year):
        rs = by_year[y]
        bbls = {r.get("borough_block_lot") or r.get("bbl") for r in rs}
        vacant = sum(norm(r.get("vacant_on_12_31")) == "YES" for r in rs)
        years.append((y, len(rs), len(bbls), vacant, 100 * vacant / len(rs) if rs else 0))
    return {"n": len(rows), "years": years,
            "categories": Counter(norm(r.get(src["category"])) or "(blank)" for r in rows).most_common(6)}


# --- ACS ---

def fetch_acs(client: httpx.Client, zips: list[str]) -> tuple[dict, str]:
    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        return {}, "CENSUS_API_KEY not set in .env: ACS check skipped (free key: https://api.census.gov/data/key_signup.html)."
    get = ",".join(ACS_VARS)

    def year_rows(year: int) -> dict[str, dict] | None:
        params = {"get": get, "for": "zip code tabulation area:" + ",".join(zips), "key": key}
        if year < 2020:
            params["in"] = "state:36"  # pre-2020 ZCTAs nest in states
        r = client.get(f"https://api.census.gov/data/{year}/acs/acs5", params=params)
        if r.status_code != 200 or not r.text.startswith("["):
            return None
        header, *data = r.json()
        out = {}
        for row in data:
            rec = dict(zip(header, row))
            out[rec["zip code tabulation area"]] = {name: float(rec[v]) if rec[v] not in (None, "") and float(rec[v]) >= 0 else None
                                                     for v, name in ACS_VARS.items()}
        return out

    early = year_rows(ACS_EARLY)
    latest_year, latest = next(((y, d) for y in ACS_LATEST_CANDIDATES if (d := year_rows(y))), (None, None))
    if not early or not latest:
        return {}, f"ACS request failed (early={bool(early)}, latest={bool(latest)})."
    return {"early_year": ACS_EARLY, "latest_year": latest_year, "early": early, "latest": latest}, ""


# --- Report ---

def pct(a, b) -> str:
    return f"{100 * (b - a) / a:+.0f}%" if a and b else "n/a"


def main():
    refresh = "--refresh" in sys.argv
    corridors = yaml.safe_load((DATA / "corridors.yaml").read_text())["corridors"]
    out = [f"# Data validation\n\nGenerated by `validate_data.py` on {TODAY.isoformat()}. "
           f"\"Long-running closure\" = inactive record whose start and end dates are >= {LONG_RUNNING_YEARS} years apart.\n"]
    totals: dict[str, dict[str, int]] = {}

    with httpx.Client(timeout=120) as client:
        for ck, c in corridors.items():
            patterns = [re.compile(p) for p in c["street_patterns"]]
            out.append(f"\n## {c['name']} ({c['neighborhood']}) - ZIPs {', '.join(c['zips'])}\n")
            totals[ck] = {}
            for sk, src in SOURCES.items():
                print(f"  {c['name']:<26} {sk:<20}", end=" ", flush=True)
                try:
                    raw = fetch(client, sk, src, ck, c, refresh)
                except httpx.HTTPError as e:
                    print(f"FAILED {e}")
                    out.append(f"\n### {src['label']} (`{src['id']}`)\n\n**Download failed:** {e}\n")
                    continue
                rows = [r for r in raw if on_corridor(r, src, patterns)]
                print(f"{len(raw):>6} fetched, {len(rows):>6} on corridor")
                totals[ck][sk] = len(rows)
                out.append(f"\n### {src['label']} (`{src['id']}`)\n")
                out.append(f"- Prefiltered rows: {len(raw)}; matched the street patterns: **{len(rows)}** "
                           f"({100 * len(rows) / len(raw):.0f}% street match)" if raw else "- **No rows.**")
                if not rows:
                    continue
                if src.get("registry"):
                    s = summarize_registry(rows, src)
                    out.append("\n| Reporting year | Filings | Distinct BBLs | Vacant 12/31 | Vacancy % |\n|---|---|---|---|---|")
                    out += [f"| {y} | {n} | {b} | {v} | {p:.1f}% |" for y, n, b, v, p in s["years"]]
                else:
                    s = summarize_licenses(rows, src)
                    rng = f"{s['start_range'][0]}-{s['start_range'][1]}" if s["start_range"] else "no start dates"
                    out.append(f"- Start dates: {rng}; inactive: {s['inactive']}; "
                               f"long-running closures: **{len(s['long_running_closures'])}**")
                    totals[ck][sk + "_closures"] = len(s["long_running_closures"])
                    if s["long_running_closures"]:
                        out.append("\n| Business | Category | Opened | Closed | Address |\n|---|---|---|---|---|")
                        out += [f"| {n} | {cat} | {a} | {b} | {addr} |" for n, cat, a, b, addr in s["long_running_closures"][:8]]
                out.append("\n- Top categories: " + ", ".join(f"{k} ({v})" for k, v in s["categories"]))

        acs, acs_note = fetch_acs(client, sorted({z for c in corridors.values() for z in c["zips"]}))

    # ACS table
    out.append("\n## ACS 5-year by ZCTA\n")
    if acs_note:
        out.append(acs_note)
    else:
        e, l = acs["early_year"], acs["latest_year"]
        out.append(f"Early = {e - 4}-{e}, latest = {l - 4}-{l} ACS 5-year. Dollar values are nominal (not inflation-adjusted).\n")
        out.append(f"| Corridor | ZCTA | Rent {e} | Rent {l} | Change | Income {e} | Income {l} | Change | Pop {e} | Pop {l} |\n|---|---|---|---|---|---|---|---|---|---|")
        for c in corridors.values():
            for z in c["zips"]:
                a, b = acs["early"].get(z, {}), acs["latest"].get(z, {})
                f = lambda d, k: f"{d.get(k):,.0f}" if d.get(k) else "n/a"
                out.append(f"| {c['name']} | {z} | {f(a, 'median_gross_rent')} | {f(b, 'median_gross_rent')} | "
                           f"{pct(a.get('median_gross_rent'), b.get('median_gross_rent'))} | {f(a, 'median_household_income')} | "
                           f"{f(b, 'median_household_income')} | {pct(a.get('median_household_income'), b.get('median_household_income'))} | "
                           f"{f(a, 'population')} | {f(b, 'population')} |")

    # Coverage overview, first in the file
    keys = list(SOURCES)
    overview = ["\n## Coverage overview (records on corridor)\n",
                "| Corridor | " + " | ".join(keys) + " | Long-running closures (all license sources) |",
                "|---" * (len(keys) + 2) + "|"]
    for ck, c in corridors.items():
        t = totals[ck]
        closures = sum(v for k, v in t.items() if k.endswith("_closures"))
        flag = " ⚠ thin" if closures < 15 else ""
        overview.append(f"| {c['name']} | " + " | ".join(str(t.get(k, "-")) for k in keys) + f" | {closures}{flag} |")
    out[1:1] = overview

    (DATA / "VALIDATION.md").write_text("\n".join(out) + "\n")
    print(f"\nWrote {DATA / 'VALIDATION.md'}")


if __name__ == "__main__":
    main()
