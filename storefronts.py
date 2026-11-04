"""Query functions over data/storefronts.duckdb: the data layer the Researchers' tools call.

The counterpart of the starter's wikipedia.py. Every function returns a plain dict:
    {"tool", "source", "params", "rows", "total_rows", "note"}
and app.py stamps it with an evidence_id (E1, E2, ...) through EvidenceLog before the model sees it.
Empty results come back as rows: [] with a note saying why, never as an exception.
"""

import json
import re
from datetime import date
from pathlib import Path

import duckdb
import yaml

from build_data import house_number, split_address

HERE = Path(__file__).parent
DB_PATH = HERE / "data" / "storefronts.duckdb"
MAX_ROWS = 25

_con = duckdb.connect(str(DB_PATH), read_only=True, config={"enable_external_access": False})
CORRIDORS = yaml.safe_load((HERE / "data" / "corridors.yaml").read_text())["corridors"]
CATEGORIES = {c["key"]: c["label"] for c in yaml.safe_load((HERE / "data" / "categories.yaml").read_text())["categories"]}

DATASETS = {
    "dcwp_current": "NYC DCWP Issued Licenses (data.cityofnewyork.us/d/w7w3-xahh)",
    "dcwp_historical": "NYC DCWP Historical Licenses, legacy export Oct 2013 (data.cityofnewyork.us/d/m4ph-grrm)",
    "nys_retail_food": "NYS Retail Food Stores (data.ny.gov/d/9a8c-vfzj)",
    "sla_active": "NYS Liquor Authority Active Licenses (data.ny.gov/d/9s3h-dpkz)",
    "sla_inactive": "NYS Liquor Authority Inactive Licenses (data.ny.gov/d/6dg3-2z7i)",
    "storefront_registry": "NYC Storefronts Reported Vacant or Not (data.cityofnewyork.us/d/92iy-9c3n)",
    "acs": "U.S. Census ACS 5-year, tables B25064/B19013/B01003 by ZCTA (api.census.gov)",
}
# Short form for multi-source tools, so the report's Sources list stays readable. Full names are in DATASETS.
LICENSE_SOURCES = ("License data: NYC DCWP (w7w3-xahh, m4ph-grrm), NYS Retail Food Stores (9a8c-vfzj), "
                   "NYS Liquor Authority (9s3h-dpkz, 6dg3-2z7i)")
REGISTRY_SOURCE = "NYC Storefront Registry (92iy-9c3n)"

# What each category's counts can and can't show. Returned in `note` whenever a category is queried.
COVERAGE = {
    "laundromat_drycleaner": "Laundries appear almost only in the DCWP legacy export, which ends Oct 2013. "
                             "Counts after 2013 are NOT observed (not a real drop), and most laundromats still "
                             "open in 2013 have status 'unknown'.",
    "restaurant": "Restaurants are seen mainly through liquor licenses (SLA), whose dates restart with each license "
                  "and whose inactive list starts around 2012. Restaurants without a liquor license are missing, "
                  "and years open are underestimated.",
    "bar": "Bars are seen through SLA liquor licenses only (history starts ~2012; dates restart with each license).",
    "cafe_coffee_bakery": "Cafés are only visible if they hold a liquor, sidewalk-café or food-store license; most don't.",
    "salon_barber": "Salons and barbers are not licensed by DCWP, SLA or NYS food; they are essentially absent.",
    "bodega_deli": "Bodegas/delis are identified mostly through tobacco retail licenses (DCWP), plus NYS food-store and "
                   "SLA grocery-beer licenses. A bodega without a tobacco or beer license may be missing.",
    "liquor_store": "Liquor stores are seen through SLA licenses (history ~2012 on).",
    "chain": "Chains are identified by name from any license source.",
}
GENERAL_NOTE = ("Licensed businesses only: industries no agency licenses (most retail, salons, barbers) are under-counted. "
                "Status 'unknown' = last seen current in the Oct 2013 legacy export.")


# --- Evidence ---

class EvidenceLog:
    """Numbers every tool result (E1, E2, ...) within one research run, so reports can cite them."""

    def __init__(self):
        self.items: list[dict] = []

    def add(self, result: dict, agent: str = "") -> dict:
        result = {"evidence_id": f"E{len(self.items) + 1}", **result}
        self.items.append({"evidence_id": result["evidence_id"], "tool": result.get("tool"), "agent": agent,
                           "source": result.get("source"), "params": result.get("params"),
                           "url": result.get("url")})
        return result

    def ids(self) -> set[str]:
        return {e["evidence_id"] for e in self.items}

    def table(self) -> str:
        """The E# -> source table the Writer sees."""
        lines = []
        for e in self.items:
            params = ", ".join(f"{k}={v}" for k, v in (e["params"] or {}).items() if v not in (None, ""))
            where = e["url"] or e["source"]
            lines.append(f"[{e['evidence_id']}] {e['tool']}({params}) - {where}")
        return "\n".join(lines)


def to_json(result: dict) -> str:
    return json.dumps(result, default=str)


# --- Helpers ---

def _query(sql: str, params: list | None = None) -> list[dict]:
    cur = _con.cursor()
    rel = cur.execute(sql, params or [])
    cols = [d[0] for d in rel.description]
    rows = []
    for row in rel.fetchall():
        rec = {}
        for c, v in zip(cols, row):
            if hasattr(v, "date") and callable(v.date):
                v = v.date().isoformat()
            elif isinstance(v, date):
                v = v.isoformat()
            elif isinstance(v, float):
                v = round(v, 3)
            rec[c] = v
        rows.append(rec)
    return rows


def _result(tool: str, params: dict, rows: list[dict], note: str, source: str, max_rows: int = MAX_ROWS) -> dict:
    out = {"tool": tool, "source": source, "params": params, "total_rows": len(rows), "rows": rows[:max_rows]}
    if len(rows) > max_rows:
        note = f"Showing {max_rows} of {len(rows)} rows. " + note
    if not rows:
        note = "No matching rows. " + note
    out["note"] = note.strip()
    return out


def resolve_corridor(corridor: str) -> str | None:
    """Accept a corridor key ('myrtle_ave') or a name ('Myrtle Ave', 'Nostrand', 'frederick douglass')."""
    if not corridor:
        return None
    c = corridor.strip().lower()
    if c in CORRIDORS:
        return c
    for key, meta in CORRIDORS.items():
        hay = f"{key} {meta['name']} {meta['neighborhood']}".lower()
        if c in hay or all(w in hay for w in re.findall(r"[a-z0-9]+", c)):
            return key
    return None


def _bad_corridor(tool: str, corridor: str, params: dict) -> dict:
    names = ", ".join(f"{k} ({v['name']}, {v['neighborhood']})" for k, v in CORRIDORS.items())
    return _result(tool, params, [], f"Unknown corridor '{corridor}'. The data covers only these pilot corridors: {names}.",
                   source="data/corridors.yaml")


def _category_note(category: str | None) -> str:
    return COVERAGE.get(category or "", "")


def resolve_category(category: str | None) -> str | None:
    if not category:
        return None
    c = category.strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    if c in CATEGORIES:
        return c
    aliases = {"laundromat": "laundromat_drycleaner", "laundry": "laundromat_drycleaner", "dry_cleaner": "laundromat_drycleaner",
               "bodega": "bodega_deli", "deli": "bodega_deli", "corner_store": "bodega_deli", "cafe": "cafe_coffee_bakery",
               "coffee": "cafe_coffee_bakery", "bakery": "cafe_coffee_bakery", "salon": "salon_barber", "barber": "salon_barber",
               "grocery": "grocery_supermarket", "supermarket": "grocery_supermarket", "phone": "phone_electronics",
               "electronics": "phone_electronics", "pawn": "secondhand_pawn", "liquor": "liquor_store", "pharmacy": "pharmacy",
               "restaurants": "restaurant", "bars": "bar", "chains": "chain"}
    for k, v in aliases.items():
        if k in c:
            return v
    return c  # unknown: the query returns no rows and the note lists valid categories


def _active_in_year_sql(year) -> str:
    """SQL predicate: business was operating at some point in `year` (an int, or a column name like 'yr')."""
    year_param = str(int(year)) if not isinstance(year, str) else year
    return (f"(start_date IS NOT NULL AND year(start_date) <= {year_param} AND "
            f"(status = 'open' OR (status IN ('closed','lapsed') AND year(end_date) >= {year_param}) "
            f" OR (status = 'unknown' AND year(last_seen) >= {year_param})))")


# --- Tools ---

def list_corridors() -> dict:
    rows = _query("""
        SELECT c.corridor, c.name, c.neighborhood, c.borough, c.zips,
               count(b.business_id) AS businesses,
               min(year(b.start_date)) AS earliest_record_year,
               (SELECT min(year) FROM vacancy v WHERE v.corridor = c.corridor) AS registry_from,
               (SELECT max(year) FROM vacancy v WHERE v.corridor = c.corridor AND NOT partial_year) AS registry_to
        FROM corridors c LEFT JOIN businesses b USING (corridor)
        GROUP BY ALL ORDER BY c.corridor""")
    return _result("list_corridors", {}, rows,
                   "License records run from the 1990s to today; storefront registry (vacancy) from 2019. " + GENERAL_NOTE,
                   source="data/corridors.yaml; " + LICENSE_SOURCES)


def corridor_summary(corridor: str, start_year: int = 2013, end_year: int = 2025) -> dict:
    params = {"corridor": corridor, "start_year": start_year, "end_year": end_year}
    ck = resolve_corridor(corridor)
    if not ck:
        return _bad_corridor("corridor_summary", corridor, params)
    rows = _query(f"""
        WITH b AS (SELECT * FROM businesses WHERE corridor = ?)
        SELECT category,
               count(*) FILTER (WHERE {_active_in_year_sql(start_year)}) AS active_start_year,
               count(*) FILTER (WHERE {_active_in_year_sql(end_year)}) AS active_end_year,
               count(*) FILTER (WHERE year(start_date) BETWEEN ? AND ?) AS openings,
               count(*) FILTER (WHERE status = 'closed' AND year(end_date) BETWEEN ? AND ?) AS closures,
               count(*) FILTER (WHERE status = 'closed' AND year(end_date) BETWEEN ? AND ? AND years_open >= 10) AS long_running_closures
        FROM b GROUP BY category ORDER BY closures DESC, active_start_year DESC""",
                  [ck, start_year, end_year, start_year, end_year, start_year, end_year])
    notes = [COVERAGE[c] for c in ("laundromat_drycleaner", "restaurant") if any(r["category"] == c for r in rows)]
    return _result("corridor_summary", {**params, "corridor": ck}, rows,
                   f"Counts of licensed businesses in {CORRIDORS[ck]['name']}. 'active_*' = operating at some point that year. "
                   + " ".join(notes) + " " + GENERAL_NOTE, source=LICENSE_SOURCES)


def find_closures(corridor: str, category: str | None = None, min_years: float = 10,
                  since_year: int | None = None) -> dict:
    params = {"corridor": corridor, "category": category, "min_years": min_years, "since_year": since_year}
    ck = resolve_corridor(corridor)
    if not ck:
        return _bad_corridor("find_closures", corridor, params)
    cat = resolve_category(category)
    sql = """SELECT name, category, address, year(start_date) AS opened, closed_year, years_open,
                    start_reliable, replaced_by_name, replaced_by_category, replacement_basis,
                    registry_after, record_ids
             FROM closures WHERE corridor = ? AND coalesce(years_open, 0) >= ?"""
    args: list = [ck, min_years]
    if cat:
        sql += " AND category = ?"
        args.append(cat)
    if since_year:
        sql += " AND closed_year >= ?"
        args.append(since_year)
    rows = _query(sql + " ORDER BY closed_year DESC, years_open DESC", args)
    note = (f"Closed = every license for the business is inactive (lapses < 12 months ago are excluded). "
            f"years_open runs from the earliest license to the last; start_reliable=false means the start date comes "
            f"from a liquor license and understates tenure. {_category_note(cat)} "
            f"Closure dates mark when licenses ended, not why the business closed.")
    if cat and cat not in CATEGORIES:
        note = f"Unknown category '{category}'. Valid: {', '.join(CATEGORIES)}. " + note
    return _result("find_closures", {**params, "corridor": ck, "category": cat}, rows, note, source=LICENSE_SOURCES)


def category_trend(corridor: str, category: str, start_year: int = 2005, end_year: int = 2025) -> dict:
    params = {"corridor": corridor, "category": category, "start_year": start_year, "end_year": end_year}
    ck = resolve_corridor(corridor)
    if not ck:
        return _bad_corridor("category_trend", corridor, params)
    cat = resolve_category(category)
    rows = _query(f"""
        WITH years AS (SELECT range AS yr FROM range(?, ? + 1)),
             b AS (SELECT * FROM businesses WHERE corridor = ? AND category = ?)
        SELECT yr AS year,
               (SELECT count(*) FROM b WHERE {_active_in_year_sql('yr')}) AS active,
               (SELECT count(*) FROM b WHERE year(start_date) = yr) AS openings,
               (SELECT count(*) FROM b WHERE status = 'closed' AND year(end_date) = yr) AS closures
        FROM years ORDER BY yr""", [start_year, end_year, ck, cat])
    if all(r["active"] == 0 and r["openings"] == 0 for r in rows):
        rows = []
    return _result("category_trend", {**params, "corridor": ck, "category": cat}, rows,
                   f"Yearly count of licensed {CATEGORIES.get(cat, cat)} businesses. {_category_note(cat)} {GENERAL_NOTE}",
                   source=LICENSE_SOURCES, max_rows=40)


def _find_address_key(address: str) -> tuple[str | None, str]:
    """'1372 Nostrand Ave' -> 'nostrand_ave|1372'. Returns (key, explanation)."""
    num, street = split_address(address)
    street = re.sub(r",.*$", "", street)  # drop ", Brooklyn NY 11226"
    for ck, c in CORRIDORS.items():
        if any(re.search(p, street) for p in c["street_patterns"]):
            # Myrtle Ave spans Brooklyn and Queens ZIPs: try the more specific (hyphenated Queens) form first.
            candidates = sorted({house_number(num, z) for z in c["zips"]} - {None}, key=len, reverse=True)
            for hn in candidates:
                key = f"{ck}|{hn}"
                if _query("SELECT 1 FROM storefront_timeline WHERE address_key = ? LIMIT 1", [key]):
                    return key, ""
            return None, f"No records at house number {num or '?'} on {c['name']}."
    return None, "That street is not one of the pilot corridors."


def storefront_timeline(address: str) -> dict:
    params = {"address": address}
    key, why = _find_address_key(address)
    if not key:
        return _result("storefront_timeline", params, [], why + " Give a house number and corridor street, e.g. '1372 Nostrand Ave'.",
                       source=LICENSE_SOURCES + "; " + REGISTRY_SOURCE)
    rows = _query("""SELECT seq, kind, occupant, category, status, start_year, end_year, start_reliable, sources, address
                     FROM storefront_timeline WHERE address_key = ? ORDER BY seq""", [key])
    return _result("storefront_timeline", {**params, "address_key": key}, rows,
                   "Occupants in order. kind='business' rows come from licenses (end_year empty = still open or status "
                   "unknown); kind='registry' rows are storefront-registry spells (2019+), which give only a broad activity "
                   "or 'Vacant'. Several shops at one house number share this timeline.",
                   source=LICENSE_SOURCES + "; " + REGISTRY_SOURCE, max_rows=40)


def replacements(corridor: str, closed_category: str | None = None, since_year: int | None = None,
                 long_running_only: bool = True) -> dict:
    params = {"corridor": corridor, "closed_category": closed_category, "since_year": since_year,
              "long_running_only": long_running_only}
    ck = resolve_corridor(corridor)
    if not ck:
        return _bad_corridor("replacements", corridor, params)
    cat = resolve_category(closed_category)
    where, args = "corridor = ?", [ck]
    if cat:
        where += " AND category = ?"
        args.append(cat)
    if since_year:
        where += " AND closed_year >= ?"
        args.append(since_year)
    if long_running_only:
        where += " AND long_running"
    rows = _query(f"""
        SELECT category AS closed_category, coalesce(replaced_by_category, 'unknown') AS replaced_by,
               count(*) AS n,
               list(name || ' (' || address || ', closed ' || closed_year || ')'
                    || coalesce(' -> ' || replaced_by_name, '') ORDER BY closed_year DESC)[1:3] AS examples
        FROM closures WHERE {where}
        GROUP BY ALL ORDER BY closed_category, n DESC""", args)
    return _result("replacements", {**params, "corridor": ck, "closed_category": cat}, rows,
                   "replaced_by = category of the next licensed business at the same address (started within 12 months, "
                   "or later after a gap), else 'vacant' if the storefront registry reported it vacant after the closure, "
                   "else the registry's broad activity; 'unknown' = no later record. "
                   "A same-category replacement (e.g. bodega -> bodega) usually means a change of owner, not of use. "
                   + _category_note(cat), source=LICENSE_SOURCES + "; " + REGISTRY_SOURCE)


def vacancy_trend(corridor: str) -> dict:
    params = {"corridor": corridor}
    ck = resolve_corridor(corridor)
    if not ck:
        return _bad_corridor("vacancy_trend", corridor, params)
    rows = _query("""SELECT year, reporting_year_label, storefronts, vacant, vacancy_rate, partial_year
                     FROM vacancy WHERE corridor = ? ORDER BY year""", [ck])
    return _result("vacancy_trend", {**params, "corridor": ck}, rows,
                   "From the Local Law 157 storefront registry: ground-floor storefronts whose owners filed, and whether each "
                   "was vacant on 12/31. Starts with the 2019 reporting year; nothing earlier exists. partial_year=true means "
                   "that year is still being filed. Filing counts vary by year, so compare rates, not counts.",
                   source=DATASETS["storefront_registry"])


def rent_income_change(corridor: str) -> dict:
    params = {"corridor": corridor}
    ck = resolve_corridor(corridor)
    if not ck:
        return _bad_corridor("rent_income_change", corridor, params)
    try:
        rows = _query("SELECT * EXCLUDE (corridor) FROM acs WHERE corridor = ? ORDER BY zcta", [ck])
    except duckdb.CatalogException:
        return _result("rent_income_change", params, [], "ACS table not built (no Census API key at build time).",
                       source=DATASETS["acs"])
    return _result("rent_income_change", {**params, "corridor": ck}, rows,
                   "ACS 5-year estimates for the ZIP code areas (ZCTAs) around the corridor, not the corridor itself. "
                   "Dollars are nominal (not inflation-adjusted; prices rose ~30-35% over this period). "
                   "These describe the neighborhood; they do not explain any single closure.", source=DATASETS["acs"])


def compare_corridors(metric: str = "long_running_closure_rate", since_year: int = 2014) -> dict:
    params = {"metric": metric, "since_year": since_year}
    rows = _query(f"""
        WITH yrs AS (SELECT range AS yr FROM range(?, 2026)),
        turnover AS (
            SELECT b.corridor, yr,
                   count(*) FILTER (WHERE {_active_in_year_sql('yr')}) AS active,
                   count(*) FILTER (WHERE status = 'closed' AND year(end_date) = yr) AS closures
            FROM businesses b, yrs GROUP BY ALL),
        t AS (SELECT corridor, round(avg(closures / nullif(active, 0)), 3) AS annual_turnover_rate
              FROM turnover GROUP BY corridor),
        base AS (SELECT corridor, count(*) FILTER (WHERE {_active_in_year_sql(since_year)}) AS active_at_start
                 FROM businesses GROUP BY corridor),
        lr AS (SELECT corridor, count(*) AS long_running_closures FROM closures
               WHERE long_running AND closed_year >= ? GROUP BY corridor),
        rent AS (SELECT corridor, round(avg(median_gross_rent_pct_change), 1) AS rent_pct_change,
                        round(avg(median_household_income_pct_change), 1) AS income_pct_change
                 FROM acs GROUP BY corridor),
        vac AS (SELECT corridor,
                       arg_min(vacancy_rate, year) AS vacancy_first, arg_max(vacancy_rate, year) AS vacancy_latest
                FROM vacancy WHERE NOT partial_year GROUP BY corridor)
        SELECT c.corridor, c.name, base.active_at_start, coalesce(lr.long_running_closures, 0) AS long_running_closures,
               round(coalesce(lr.long_running_closures, 0) / nullif(base.active_at_start, 0), 3) AS long_running_closure_rate,
               t.annual_turnover_rate, rent.rent_pct_change, rent.income_pct_change,
               vac.vacancy_first, vac.vacancy_latest
        FROM corridors c LEFT JOIN base USING (corridor) LEFT JOIN lr USING (corridor) LEFT JOIN t USING (corridor)
        LEFT JOIN rent USING (corridor) LEFT JOIN vac USING (corridor)""", [since_year, since_year])
    valid = ["long_running_closure_rate", "annual_turnover_rate", "long_running_closures", "rent_pct_change",
             "income_pct_change", "vacancy_latest"]
    key = metric if metric in valid else "long_running_closure_rate"
    rows.sort(key=lambda r: (r.get(key) is None, -(r.get(key) or 0)))
    for col in ("long_running_closure_rate", "annual_turnover_rate", "rent_pct_change"):
        ranked = sorted((r for r in rows if r.get(col) is not None), key=lambda r: -r[col])
        for i, r in enumerate(ranked, 1):
            r[f"rank_{col}"] = i
    rho = {m: _spearman([r.get("rent_pct_change") for r in rows], [r.get(m) for r in rows])
           for m in ("long_running_closure_rate", "annual_turnover_rate")}
    rows.append({"corridor": "SUMMARY", "name": "Rank correlation with rent_pct_change (Spearman, n=5)",
                 "spearman_rent_vs_long_running_closure_rate": rho["long_running_closure_rate"],
                 "spearman_rent_vs_annual_turnover_rate": rho["annual_turnover_rate"]})
    note = (f"Ranked by {key}; the last row is a SUMMARY of Spearman rank correlations (+1 = same order, "
            f"0 = unrelated, -1 = opposite). Compare corridors by rates, not raw closure counts: corridors differ in size. long_running_closure_rate = closures since {since_year} of businesses open 10+ years, divided "
            f"by licensed businesses active in {since_year}. annual_turnover_rate = average yearly closures / active "
            "businesses. rent/income % change = average over the corridor's ZCTAs, ACS 2011-2015 vs latest, nominal dollars. "
            "With only 5 corridors, any rank agreement between rent change and closures is suggestive at best and is "
            "correlation, not causation.")
    if metric not in valid:
        note = f"Unknown metric '{metric}', used long_running_closure_rate. Valid: {', '.join(valid)}. " + note
    return _result("compare_corridors", params, rows, note, source=LICENSE_SOURCES + "; " + REGISTRY_SOURCE
                   + "; " + DATASETS["acs"])


def _spearman(xs: list, ys: list) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None

    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        for pos, i in enumerate(order, 1):
            r[i] = pos
        return r

    rx, ry = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    return round(1 - 6 * sum((a - b) ** 2 for a, b in zip(rx, ry)) / (n * (n * n - 1)), 2)


SQL_BLOCK = re.compile(r"\b(insert|update|delete|create|drop|alter|attach|detach|copy|export|import|install|load|"
                       r"pragma|set|call|checkpoint|read_\w+|glob|httpfs)\b", re.I)


def run_sql(query: str) -> dict:
    """Read-only SELECT over the tables, as a fallback when no other tool fits."""
    params = {"query": query}
    q = query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I) or ";" in q or SQL_BLOCK.search(q):
        return _result("run_sql", params, [], "Rejected: only a single read-only SELECT is allowed.", source="data/storefronts.duckdb")
    try:
        rows = _query(f"SELECT * FROM ({q}) LIMIT 200")
    except duckdb.Error as e:
        return _result("run_sql", params, [], f"SQL error: {e}. Tables: records, businesses, closures, "
                       "storefront_timeline, registry, vacancy, acs, corridors.", source="data/storefronts.duckdb")
    return _result("run_sql", params, rows, GENERAL_NOTE, source="data/storefronts.duckdb (built from " + LICENSE_SOURCES + "; " + REGISTRY_SOURCE + ")",
                   max_rows=50)
