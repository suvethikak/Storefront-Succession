"""Address normalization shared by the offline build (scripts/build_data.py) and the live app (storefronts.py)."""

import re

QUEENS_ZIPS = re.compile(r"^11[34]\d\d$")  # 113xx/114xx/11385 are Queens ZIPs in our pilot


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").upper()).strip()


def house_number(raw: str, zip_code: str) -> str | None:
    """Canonical house number, or None if there isn't one."""
    s = norm(raw).lstrip("*#").strip()
    if not s:
        return None
    queens = bool(QUEENS_ZIPS.match(zip_code or ""))
    if queens:
        m = re.match(r"^(\d{1,3})\s*[- ]\s*(\d{1,3}[A-Z]?)\b", s)
        if m:
            return f"{int(m.group(1))}-{m.group(2).zfill(2)}"
        m = re.match(r"^(\d{1,3})(\d{2})([A-Z]?)\b", s)  # 7601 -> 76-01, 10115 -> 101-15
        if m:
            return f"{int(m.group(1))}-{m.group(2)}{m.group(3)}"
    m = re.match(r"^(\d+)([A-Z]?)\b", s)  # 1861-71 -> 1861, 522A -> 522A
    return f"{int(m.group(1))}{m.group(2)}" if m else None


def split_address(full: str) -> tuple[str, str]:
    """'76 01 ROOSEVELT AVE' -> ('76 01', 'ROOSEVELT AVE'); '2150 8 AVE' -> ('2150', '8 AVE')."""
    s = norm(full)
    m = re.match(r"^(\d+[A-Z]?(?:\s*-\s*\d+[A-Z]?|\s+\d{2}(?=\s+\D))?)\s+(.*)$", s)
    return (m.group(1), m.group(2)) if m else ("", s)
