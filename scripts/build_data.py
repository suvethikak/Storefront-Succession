"""Offline build: raw open-data pulls -> data/storefronts.duckdb, the database the agent queries.

    uv run scripts/build_data.py            # uses cached pulls in data/raw/ (run validate_data.py first, or this fetches them)
    uv run scripts/build_data.py --refresh  # re-download every source first

Pipeline (see data/BUILD_REPORT.md for the match rates of the last run):
  1. Load each source x corridor (same cached pulls as validate_data.py) and keep on-corridor rows.
  2. Normalize addresses to address_key = "<corridor>|<house number>". Every row is already matched
     to one corridor street, so the house number is all that varies. Queens numbers are hyphenated
     (7601, 76 01 -> 76-01); Brooklyn/Manhattan ranges keep the first number (1861-71 -> 1861).
  3. Classify every record with data/categories.yaml.
  4. Merge records into businesses: same address_key and a matching name (equal, contained, or
     sharing a distinctive word). A business's span runs from its earliest start to its latest end.
  5. Decide status: open (any active record), closed (all records inactive), or unknown (only
     pre-Oct-2013 legacy records that were still current when that system's export ends).
  6. Build closures with their replacement, the storefront timeline, registry vacancy, and ACS.

Tables written:
  records            one row per source record (normalized), for citations and ad-hoc SQL
  businesses         one row per merged business: name, category, address, span, status, sources
  closures           closed businesses with years open, long_running flag, and what replaced them
  storefront_timeline ordered occupants at each address_key: businesses and registry spells
  registry           one row per storefront registry filing (vacant on 12/31, broad activity)
  vacancy            corridor x registry year: storefronts, vacant, vacancy rate
  acs                corridor x ZCTA: early vs latest rent, income, population, % change
  corridors          corridor metadata
  meta               build date and source descriptions
"""

import re
import sys
from datetime import date, timedelta
from pathlib import Path

import duckdb
import httpx
import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent.parent  # the repo root
sys.path.insert(0, str(HERE))

from scripts.validate_data import SOURCES, TODAY, fetch, fetch_acs, on_corridor, parse_date  # noqa: E402
from storefront.addresses import house_number, norm, split_address  # noqa: E402
DATA = HERE / "data"
DB_PATH = DATA / "storefronts.duckdb"

LONG_RUNNING_YEARS = 10
RENEWAL_GRACE = timedelta(days=365)  # "closed" = not renewed within 12 months
LEGACY_EXPORT_DATE = date(2013, 10, 31)  # DCWP legacy system export (status dates stop Oct 2013)

# Statuses that say the business itself ended (not just a lapsed renewal).
DEFINITE_CLOSE = {"OUT OF BUSINESS", "SURRENDERED", "REVOKED", "CLOSE", "SURRENDER/REVOCATION", "OOB/REVOKED",
                  "OOB/LICENSE ALERT", "BUSINESS PADLOCKED"}
SUPERMARKET_SQFT = 5000


# Words that don't identify a business, ignored when comparing names.
GENERIC = set("""INC CORP CORPORATION LLC LTD CO COMPANY THE DBA OF AND & NY NYC NEW YORK BROOKLYN QUEENS
HARLEM DELI GROCERY FOOD FOODS MARKET MINI MART CORP. RESTAURANT CAFE PIZZA STORE SHOP CENTER
ENTERPRISES ENTERPRISE GROUP 1 2 3 I II III INTERNATIONAL TRADING WIRELESS LAUNDROMAT LAUNDRY
CLEANERS BAR GOURMET DISCOUNT FARM FRESH 99 CENT CENTS PLUS EXPRESS SUPERMARKET""".split())


# --- Names ---

def name_key(name: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", norm(name).replace("&", " AND "))
    s = re.sub(r"\b(INC|CORP|CORPORATION|LLC|LTD|CO|DBA|THE)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def distinctive(key: str) -> set[str]:
    return {t for t in key.split() if t not in GENERIC and len(t) > 1}


def same_business(a: dict, b: dict) -> bool:
    """Do two records at the same address name the same business?"""
    for ka in a["name_keys"]:
        for kb in b["name_keys"]:
            if not ka or not kb:
                continue
            ca, cb = ka.replace(" ", ""), kb.replace(" ", "")
            if ca == cb or (min(len(ca), len(cb)) >= 6 and (ca in cb or cb in ca)):
                return True
            da, db = distinctive(ka), distinctive(kb)
            if da and db and len(da & db) / min(len(da), len(db)) >= 0.5:
                return True
    return False


# --- Categories ---

class Classifier:
    def __init__(self, path: Path):
        self.cats = yaml.safe_load(path.read_text())["categories"]
        chain_cat = next(c for c in self.cats if c["key"] == "chain")
        self.chain_patterns = [re.compile(p) for p in chain_cat["name_patterns"]] + [
            re.compile(r"\b(CVS|DUANE READE|WALGREENS?|RITE AID|ECKERD)\b")]
        self.order = {c["key"]: i for i, c in enumerate(self.cats)}
        self.labels = {c["key"]: c["label"] for c in self.cats}
        self.name_rules = [(c["key"], [re.compile(p) for p in c.get("name_patterns", [])]) for c in self.cats]
        self.source_rules = {norm(s): c["key"] for c in self.cats for s in c.get("source_categories", [])}

    def classify(self, names: list[str], raw_category: str) -> tuple[str, str]:
        """(category, basis). Name rules first, in file order, then the source's own label."""
        text = " | ".join(norm(n) for n in names if n)
        for key, patterns in self.name_rules:
            if any(p.search(text) for p in patterns):
                return key, "name"
        if norm(raw_category) in self.source_rules:
            return self.source_rules[norm(raw_category)], "source_category"
        return "other", "fallback"

    def is_chain(self, names: list[str]) -> bool:
        text = " | ".join(norm(n) for n in names if n)
        return any(p.search(text) for p in self.chain_patterns)


# --- Step 1-3: records ---

def load_records(client, corridors: dict, classifier: Classifier, refresh: bool):
    records, registry = [], []
    stats = []
    for ck, c in corridors.items():
        patterns = [re.compile(p) for p in c["street_patterns"]]
        for sk, src in SOURCES.items():
            raw = fetch(client, sk, src, ck, c, refresh)
            rows = [r for r in raw if on_corridor(r, src, patterns)]
            keyed = 0
            for r in rows:
                zip_code = str(r.get(src["zip"]) or "")[:5]
                if src.get("registry"):
                    num_raw, _ = split_address(r.get("property_street_address_or") or "")
                    num_raw = num_raw or r.get("property_number")
                elif src["building"]:
                    num_raw = r.get(src["building"])
                else:
                    num_raw, _ = split_address(r.get(src["street"]))
                hn = house_number(num_raw, zip_code)
                keyed += hn is not None
                if hn is None:
                    continue
                address_key = f"{ck}|{hn}"
                address = f"{hn} {c['name']}"
                if src.get("registry"):
                    label = str(r.get("reporting_year") or "")
                    # Labels run "2019 and 2020", "2020 and 2021", ..., "2022 and 2023", "2023", "2024":
                    # the first year keeps the sequence unique (2019 ... 2025).
                    years_in_label = re.findall(r"\d{4}", label)
                    year = int(years_in_label[0]) if years_in_label else None
                    vacant = norm(r.get("vacant_on_12_31")) == "YES"
                    activity = norm(r.get("primary_business_activity")) or "NOT REPORTED"
                    registry.append({
                        "corridor": ck, "address_key": address_key, "address": address, "zip": zip_code,
                        "reporting_year_label": label, "year": year, "vacant": vacant, "activity": activity,
                        "category": "vacant" if vacant else classifier.classify([], activity)[0],
                        "bbl": r.get("bbl") or r.get("borough_block_lot"),
                        "filed_address": norm(r.get("property_street_address_or")),
                        "source": f"{src['id']}",
                    })
                    continue
                names = [r.get(f) for f in src["name"] if r.get(f)]
                raw_cat = norm(r.get(src["category"]))
                category, basis = classifier.classify(names, raw_cat)
                if sk == "nys_retail_food" and basis == "fallback":
                    # NYS establishment codes (A = store, C = food processing) don't say what kind of store.
                    sqft = float(r.get("square_footage") or 0)
                    category, basis = ("grocery_supermarket" if sqft >= SUPERMARKET_SQFT else "bodega_deli"), "square_footage"
                start = parse_date(r.get(src["start"])) if src["start"] else None
                end = parse_date(r.get(src["end"])) if src["end"] else None
                # status: active / inactive / legacy_current (historical export still "current" in 2013)
                if src.get("all_active") is True:
                    status, end = "active", None
                elif src.get("all_active") is False:
                    status = "inactive"
                elif norm(r.get(src["status"])) in src["active_statuses"]:
                    status = "legacy_current" if sk == "dcwp_historical" else "active"
                    end = None
                else:
                    status = "inactive"
                if status == "inactive" and end and end > TODAY:  # ended before its paper expiration date
                    end = TODAY
                record_id = (r.get("license_nbr") or r.get("dca_license_number") or r.get("license_number")
                             or r.get("licensepermitid") or r.get("license_permit_id") or "")
                records.append({
                    "record_id": f"{sk}:{record_id}", "source": sk, "dataset_id": src["id"], "corridor": ck,
                    "address_key": address_key, "address": address, "zip": zip_code,
                    "name": names[0] if names else "", "alt_name": names[1] if len(names) > 1 else None,
                    "name_keys": [name_key(n) for n in names],
                    "raw_category": raw_cat, "category": category, "category_basis": basis,
                    "status": status, "raw_status": norm(r.get(src["status"])) if src["status"] else status.upper(),
                    "start_date": start, "end_date": end,
                    # SLA "original issue" dates restart with the license, so they don't date the business
                    "start_reliable": sk in ("dcwp_current", "dcwp_historical"),
                })
            stats.append({"corridor": ck, "source": sk, "fetched": len(raw), "on_corridor": len(rows), "address_keyed": keyed})
    return records, registry, stats


# --- Step 4-5: businesses ---

def merge_businesses(records: list[dict], classifier: Classifier) -> list[dict]:
    by_addr: dict[str, list[dict]] = {}
    for r in records:
        by_addr.setdefault(r["address_key"], []).append(r)

    businesses = []
    for addr, recs in by_addr.items():
        parent = list(range(len(recs)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                if same_business(recs[i], recs[j]):
                    parent[find(i)] = find(j)
        groups: dict[int, list[dict]] = {}
        for i, r in enumerate(recs):
            groups.setdefault(find(i), []).append(r)

        for group in groups.values():
            starts = [r["start_date"] for r in group if r["start_date"]]
            reliable_starts = [r["start_date"] for r in group if r["start_date"] and r["start_reliable"]]
            ends = [r["end_date"] for r in group if r["end_date"]]
            statuses = {r["status"] for r in group}
            last_end = max(ends) if ends else None
            if "active" in statuses:
                status, end = "open", None
            elif "legacy_current" in statuses and not (last_end and last_end > LEGACY_EXPORT_DATE):
                status, end = "unknown", None  # last seen current in the 2013 legacy export
            elif (last_end and last_end > TODAY - RENEWAL_GRACE
                  and not any(r["raw_status"] in DEFINITE_CLOSE for r in group)):
                status, end = "lapsed", last_end  # expired < 12 months ago: may still renew, not a closure yet
            else:
                status, end = "closed", last_end
            start = min(starts) if starts else None
            through = end or (LEGACY_EXPORT_DATE if status == "unknown" else TODAY)
            years = round((through - start).days / 365.25, 1) if start else None
            category = min((r["category"] for r in group), key=lambda k: classifier.order.get(k, 999))
            name = max((r["name"] for r in group), key=lambda n: (sum(r["name"] == n for r in group), -len(n)))
            businesses.append({
                "address_key": addr, "corridor": group[0]["corridor"], "address": group[0]["address"],
                "zip": group[0]["zip"], "name": name,
                "other_names": sorted({n for r in group for n in (r["name"], r["alt_name"]) if n and n != name}),
                "category": category, "category_label": classifier.labels.get(category, category),
                "is_chain": classifier.is_chain([n for r in group for n in (r["name"], r["alt_name"]) if n]),
                "status": status, "start_date": start, "end_date": end,
                "last_seen": end or (LEGACY_EXPORT_DATE if status == "unknown" else TODAY),
                "start_reliable": bool(reliable_starts) and min(reliable_starts) == start,
                "years_open": years,
                "sources": sorted({r["source"] for r in group}),
                "record_ids": sorted(r["record_id"] for r in group),
                "raw_categories": sorted({r["raw_category"] for r in group if r["raw_category"]}),
            })
    businesses.sort(key=lambda b: (b["address_key"], b["start_date"] or date.max))
    for i, b in enumerate(businesses, 1):
        b["business_id"] = f"B{i:05d}"
    return businesses


# --- Step 6: closures, timeline, vacancy ---

def build_closures(businesses: list[dict], registry: list[dict]) -> list[dict]:
    by_addr: dict[str, list[dict]] = {}
    for b in businesses:
        by_addr.setdefault(b["address_key"], []).append(b)
    reg_by_addr: dict[str, list[dict]] = {}
    for r in registry:
        reg_by_addr.setdefault(r["address_key"], []).append(r)

    closures = []
    for b in businesses:
        if b["status"] != "closed" or not b["end_date"]:
            continue
        end = b["end_date"]
        # Next business at this address: starts after this one did, and no earlier than 12 months before it closed.
        later = [o for o in by_addr[b["address_key"]] if o is not b and o["start_date"]
                 and o["start_date"] > (b["start_date"] or date.min) and o["start_date"] >= end - RENEWAL_GRACE]
        nxt = min(later, key=lambda o: o["start_date"]) if later else None
        # Registry: what was reported at this storefront in the years after the closure.
        after = sorted((r for r in reg_by_addr.get(b["address_key"], []) if r["year"] and r["year"] >= end.year),
                       key=lambda r: r["year"])
        vacant_years = sorted({r["year"] for r in after if r["vacant"]})
        gap_days = (nxt["start_date"] - end).days if nxt else None

        if nxt and gap_days <= RENEWAL_GRACE.days:
            basis, rep_cat, rep_name = "license", nxt["category"], nxt["name"]
        elif after and after[0]["vacant"]:
            basis, rep_cat, rep_name = "registry", "vacant", None
        elif nxt:
            basis, rep_cat, rep_name = "license_after_gap", nxt["category"], nxt["name"]
        elif after:
            basis, rep_cat, rep_name = "registry", after[0]["category"], None
        else:
            basis, rep_cat, rep_name = "none", None, None

        closures.append({
            "business_id": b["business_id"], "corridor": b["corridor"], "address_key": b["address_key"],
            "address": b["address"], "name": b["name"], "category": b["category"], "is_chain": b["is_chain"],
            "start_date": b["start_date"], "closed_date": end, "closed_year": end.year,
            "years_open": b["years_open"], "start_reliable": b["start_reliable"],
            "long_running": bool(b["years_open"] and b["years_open"] >= LONG_RUNNING_YEARS),
            "replaced_by_business_id": nxt["business_id"] if nxt and basis.startswith("license") else None,
            "replaced_by_name": rep_name, "replaced_by_category": rep_cat,
            "replaced_by_chain": bool(nxt and basis.startswith("license") and nxt["is_chain"]),
            "replaced_by_start": nxt["start_date"] if nxt and basis.startswith("license") else None,
            "gap_days": gap_days if basis.startswith("license") else None,
            "replacement_basis": basis,
            "registry_vacant_years": vacant_years,
            "registry_after": [f"{r['year']}: {'VACANT' if r['vacant'] else r['activity']}" for r in after][:6],
            "sources": b["sources"], "record_ids": b["record_ids"],
        })
    return closures


def registry_spells(regs: list[dict]) -> list[dict]:
    """Collapse consecutive registry years with the same state into one spell per address."""
    per_year: dict[int, dict] = {}
    for r in sorted(regs, key=lambda r: r["year"] or 0):
        if not r["year"]:
            continue
        cur = per_year.setdefault(r["year"], {"vacant": True, "activities": set()})
        cur["vacant"] &= r["vacant"]  # several filings for one storefront-year: vacant only if all say so
        if not r["vacant"]:
            cur["activities"].add(r["activity"])
    spells = []
    for y in sorted(per_year):
        acts = sorted(per_year[y]["activities"])
        # One house number can hold several storefronts; more than two activities = a multi-shop building.
        state = ("VACANT" if per_year[y]["vacant"] else " / ".join(acts) if len(acts) <= 2
                 else f"MULTIPLE STOREFRONTS ({len(acts)} ACTIVITIES)")
        if spells and spells[-1]["state"] == state and spells[-1]["end_year"] == y - 1:
            spells[-1]["end_year"] = y
        else:
            spells.append({"state": state, "start_year": y, "end_year": y})
    return spells


def build_timeline(businesses: list[dict], registry: list[dict], classifier: Classifier) -> list[dict]:
    reg_by_addr: dict[str, list[dict]] = {}
    for r in registry:
        reg_by_addr.setdefault(r["address_key"], []).append(r)
    rows = []
    for b in businesses:
        rows.append({
            "address_key": b["address_key"], "corridor": b["corridor"], "address": b["address"],
            "kind": "business", "business_id": b["business_id"], "occupant": b["name"],
            "category": b["category"], "status": b["status"],
            "start_year": b["start_date"].year if b["start_date"] else None,
            "end_year": b["end_date"].year if b["end_date"] else None,
            "start_reliable": b["start_reliable"], "sources": b["sources"],
        })
    for addr, regs in reg_by_addr.items():
        r0 = regs[0]
        for s in registry_spells(regs):
            cat = ("vacant" if s["state"] == "VACANT" else "other" if s["state"].startswith("MULTIPLE")
                   else classifier.classify([], s["state"].split(" / ")[0])[0])
            rows.append({
                "address_key": addr, "corridor": r0["corridor"], "address": r0["address"],
                "kind": "registry", "business_id": None,
                "occupant": "Vacant" if s["state"] == "VACANT" else f"Registry: {s['state'].title()}",
                "category": cat, "status": "registry",
                "start_year": s["start_year"], "end_year": s["end_year"],
                "start_reliable": True, "sources": ["storefront_registry"],
            })
    rows.sort(key=lambda r: (r["address_key"], r["start_year"] or 0, r["kind"] != "business"))
    seq = {}
    for r in rows:
        seq[r["address_key"]] = seq.get(r["address_key"], 0) + 1
        r["seq"] = seq[r["address_key"]]
    return rows


def build_vacancy(registry: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(registry)
    # One storefront-year can have several filings (amended returns): vacant only if all of them say so.
    sf = df.groupby(["corridor", "year", "reporting_year_label", "address_key"], as_index=False)["vacant"].all()
    out = sf.groupby(["corridor", "year", "reporting_year_label"], as_index=False).agg(
        storefronts=("address_key", "count"), vacant=("vacant", "sum"))
    out["vacancy_rate"] = (out["vacant"] / out["storefronts"]).round(3)
    # The newest reporting year is still being filed: flag years with far fewer filings than usual.
    typical = out.groupby("corridor")["storefronts"].transform("median")
    out["partial_year"] = out["storefronts"] < 0.5 * typical
    return out.sort_values(["corridor", "year"])


def build_acs(client, corridors: dict) -> tuple[pd.DataFrame, str]:
    zips = sorted({z for c in corridors.values() for z in c["zips"]})
    acs, note = fetch_acs(client, zips)
    if note:
        return pd.DataFrame(), note
    rows = []
    for ck, c in corridors.items():
        for z in c["zips"]:
            a, b = acs["early"].get(z, {}), acs["latest"].get(z, {})
            row = {"corridor": ck, "zcta": z,
                   "early_period": f"{acs['early_year'] - 4}-{acs['early_year']}",
                   "latest_period": f"{acs['latest_year'] - 4}-{acs['latest_year']}"}
            for k in ("median_gross_rent", "median_household_income", "population"):
                row[f"{k}_early"], row[f"{k}_latest"] = a.get(k), b.get(k)
                row[f"{k}_pct_change"] = round(100 * (b[k] - a[k]) / a[k], 1) if a.get(k) and b.get(k) else None
            rows.append(row)
    return pd.DataFrame(rows), ""


# --- Write ---

def to_df(rows: list[dict], drop=()) -> pd.DataFrame:
    df = pd.DataFrame([{k: v for k, v in r.items() if k not in drop} for r in rows])
    for col in df.columns:
        if col.endswith("_date") or col in ("last_seen", "replaced_by_start"):
            df[col] = pd.to_datetime(df[col])
        elif col.endswith("_year") or col in ("year", "gap_days"):
            df[col] = df[col].astype("Int64")
    return df


def main():
    refresh = "--refresh" in sys.argv
    corridors = yaml.safe_load((DATA / "corridors.yaml").read_text())["corridors"]
    classifier = Classifier(DATA / "categories.yaml")

    with httpx.Client(timeout=120) as client:
        print("Loading records ...")
        records, registry, stats = load_records(client, corridors, classifier, refresh)
        print("Fetching ACS ...")
        acs, acs_note = build_acs(client, corridors)

    print(f"  {len(records)} license records, {len(registry)} registry filings")
    businesses = merge_businesses(records, classifier)
    closures = build_closures(businesses, registry)
    timeline = build_timeline(businesses, registry, classifier)
    vacancy = build_vacancy(registry)
    print(f"  {len(businesses)} businesses, {len(closures)} closures "
          f"({sum(c['long_running'] for c in closures)} long-running), {len(timeline)} timeline rows")

    DB_PATH.unlink(missing_ok=True)
    con = duckdb.connect(str(DB_PATH))
    tables = {
        "records": to_df(records, drop=("name_keys",)),
        "businesses": to_df(businesses),
        "closures": to_df(closures),
        "storefront_timeline": to_df(timeline),
        "registry": to_df(registry),
        "vacancy": vacancy,
        "corridors": pd.DataFrame([{"corridor": k, "name": c["name"], "neighborhood": c["neighborhood"],
                                    "borough": c["borough"], "zips": c["zips"]} for k, c in corridors.items()]),
        "meta": pd.DataFrame([{"key": "built_on", "value": TODAY.isoformat()},
                              {"key": "legacy_export_date", "value": LEGACY_EXPORT_DATE.isoformat()},
                              {"key": "long_running_years", "value": str(LONG_RUNNING_YEARS)},
                              {"key": "acs_note", "value": acs_note or "ok"}]
                             + [{"key": f"source:{k}", "value": f"{s['label']} ({s['domain']}/d/{s['id']})"}
                                for k, s in SOURCES.items()]),
    }
    if not acs.empty:
        tables["acs"] = acs
    for name, df in tables.items():
        con.register("df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM df")
        con.unregister("df")
    con.close()
    print(f"Wrote {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.1f} MB)")

    write_report(stats, records, registry, businesses, closures, corridors, classifier, acs_note)


def write_report(stats, records, registry, businesses, closures, corridors, classifier, acs_note):
    st = pd.DataFrame(stats)
    by_src = st.groupby("source")[["fetched", "on_corridor", "address_keyed"]].sum()
    lines = [f"# Build report\n\nGenerated by `scripts/build_data.py` on {TODAY.isoformat()}.\n",
             "## Match rates\n",
             "`on_corridor` = matched a corridor's ZIP and street patterns; `address_keyed` = also got a house number.\n",
             "| Source | Fetched | On corridor | Address keyed | Keyed % |", "|---|---|---|---|---|"]
    for src, r in by_src.iterrows():
        lines.append(f"| {src} | {r.fetched} | {r.on_corridor} | {r.address_keyed} | "
                     f"{100 * r.address_keyed / r.on_corridor:.1f}% |" if r.on_corridor else f"| {src} | {r.fetched} | 0 | 0 | - |")

    lic_addrs = {b["address_key"] for b in businesses}
    reg_addrs = {r["address_key"] for r in registry}
    lines += ["", f"- Registry storefront addresses that also have at least one licensed business: "
                  f"{len(reg_addrs & lic_addrs)} / {len(reg_addrs)} ({100 * len(reg_addrs & lic_addrs) / max(len(reg_addrs), 1):.0f}%)",
              f"- License records merged into businesses: {len(records)} records -> {len(businesses)} businesses",
              f"- Businesses whose start date comes from a DCWP license (reliable tenure): "
              f"{sum(b['start_reliable'] for b in businesses)} / {len(businesses)}"]
    basis = pd.Series([r["category_basis"] for r in records]).value_counts()
    lines.append(f"- Category assigned by name: {basis.get('name', 0)}, by source label: {basis.get('source_category', 0)}, "
                 f"by NYS square footage: {basis.get('square_footage', 0)}, "
                 f"fallback 'other': {basis.get('fallback', 0)}")
    other = pd.Series([r["raw_category"] for r in records if r["category_basis"] == "fallback"]).value_counts().head(10)
    if len(other):
        lines.append("- Most common raw labels that fell through to 'other': "
                     + ", ".join(f"{k or '(blank)'} ({v})" for k, v in other.items()))
    if acs_note:
        lines.append(f"- ACS: {acs_note}")

    lines += ["", "## Per corridor\n",
              "| Corridor | Businesses | Open | Closed | Lapsed <12 mo | Unknown (legacy) | Closures 10+ yrs | ...with reliable start | Replacement known |",
              "|---|---|---|---|---|---|---|---|---|"]
    for ck, c in corridors.items():
        bs = [b for b in businesses if b["corridor"] == ck]
        cs = [x for x in closures if x["corridor"] == ck]
        lr = [x for x in cs if x["long_running"]]
        lines.append(f"| {c['name']} | {len(bs)} | {sum(b['status'] == 'open' for b in bs)} | "
                     f"{sum(b['status'] == 'closed' for b in bs)} | {sum(b['status'] == 'lapsed' for b in bs)} | "
                     f"{sum(b['status'] == 'unknown' for b in bs)} | "
                     f"{len(lr)} | {sum(x['start_reliable'] for x in lr)} | "
                     f"{sum(x['replacement_basis'] != 'none' for x in cs)} / {len(cs)} |")

    lines += ["", "## Long-running closures by category -> replacement\n",
              "| Closed category | Closures | Replaced by (top) |", "|---|---|---|"]
    lr = pd.DataFrame([x for x in closures if x["long_running"]])
    if not lr.empty:
        for cat, g in lr.groupby("category"):
            reps = g["replaced_by_category"].fillna("unknown").value_counts().head(4)
            lines.append(f"| {classifier.labels.get(cat, cat)} | {len(g)} | "
                         + ", ".join(f"{classifier.labels.get(k, k)} ({v})" for k, v in reps.items()) + " |")
    (DATA / "BUILD_REPORT.md").write_text("\n".join(lines) + "\n")
    print(f"Wrote {DATA / 'BUILD_REPORT.md'}")


if __name__ == "__main__":
    main()
