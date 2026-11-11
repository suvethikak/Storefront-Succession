"""Web search and page fetch for the Press Researcher: the only source that can say WHY a business closed.

web_search uses Gemini on Vertex AI with Google Search grounding. Gemini writes an answer, and the grounding
metadata says which sentences each web source supports. We return only those supported sentences, per source,
with the source's real URL; the model's unsupported text is dropped.

Both functions return the same dict shape as storefronts.py, so app.py can stamp them with evidence IDs.
"""

import ipaddress
import re
import socket
import time
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urlparse

import google.auth
import httpx
from google import genai
from google.genai import types

SEARCH_MODEL = "gemini-3.5-flash"
MAX_SOURCES = 8
PAGE_CHARS = 6000
UA = {"User-Agent": "Mozilla/5.0 (compatible; StorefrontSuccessionResearch/0.1)"}


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    _, project = google.auth.default()
    return genai.Client(vertexai=True, project=project, location="global")


def _resolve(url: str) -> str:
    """Grounding links are vertexaisearch redirects; follow one hop to get the publisher's URL."""
    if "grounding-api-redirect" not in url:
        return url
    try:
        r = httpx.head(url, follow_redirects=False, timeout=8, headers=UA)
        return r.headers.get("location", url)
    except httpx.HTTPError:
        return url


def web_search(query: str) -> dict:
    params = {"query": query}
    prompt = (f"Search the web and report what news articles, local blogs or official reports say about: {query}\n"
              "Focus on New York City storefront businesses. Quote specific facts with dates. "
              "If nothing specific is found, say so.")
    # Thinking off: ~4 s per search instead of 20-40 s, with about as many sources.
    config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0,
                                         thinking_config=types.ThinkingConfig(thinking_budget=0))
    resp, error = None, None
    for attempt in range(3):
        try:
            resp = _client().models.generate_content(model=SEARCH_MODEL, contents=prompt, config=config)
            break
        except Exception as e:  # 429 quota bursts are common: back off briefly, then give up
            error = e
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                break
            time.sleep(2 + 3 * attempt)
    if resp is None:
        e = error
        return {"tool": "web_search", "source": "Google Search (via Gemini grounding)", "params": params, "rows": [],
                "total_rows": 0, "note": f"Search failed: {type(e).__name__}: {str(e)[:200]}"}

    gm = resp.candidates[0].grounding_metadata if resp.candidates else None
    chunks = list(getattr(gm, "grounding_chunks", None) or [])
    supported: dict[int, list[str]] = {}
    for s in getattr(gm, "grounding_supports", None) or []:
        text = re.sub(r"\*+", "", s.segment.text or "").strip()
        for i in s.grounding_chunk_indices or []:
            if text and text not in supported.setdefault(i, []):
                supported[i].append(text)

    rows, seen = [], set()
    for i, ch in enumerate(chunks):
        if not ch.web or i not in supported:
            continue  # a source no sentence relies on adds nothing
        url = _resolve(ch.web.uri)
        if url in seen:
            continue
        seen.add(url)
        rows.append({"title": ch.web.title, "url": url, "says": " ".join(supported[i])[:1200]})
        if len(rows) >= MAX_SOURCES:
            break

    note = ("Each row is one web source and the statements it supports (from Google Search grounding). Treat these as "
            "claims by that source, not verified facts; prefer news outlets and official reports over listings or "
            "directories. Use fetch_page on a URL to read it in full.")
    return {"tool": "web_search", "source": "Google Search (via Gemini grounding)", "params": params,
            "search_queries": list(getattr(gm, "web_search_queries", None) or []), "rows": rows,
            "total_rows": len(rows), "url": " ; ".join(r["url"] for r in rows[:5]) or None,
            "note": (note if rows else "No web sources found for this query.")}


class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form", "aside"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip, self.title = [], 0, ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        self.skip += tag in self.SKIP
        self._in_title = tag == "title"

    def handle_endtag(self, tag):
        self.skip -= tag in self.SKIP and self.skip > 0
        self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip and data.strip():
            self.parts.append(data.strip())


def _public_host(url: str) -> bool:
    """Refuse URLs that point inside the network (e.g. Cloud Run's metadata server)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(p.hostname, None)}
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(a).is_global for a in addrs)


def fetch_page(url: str) -> dict:
    params = {"url": url}
    base = {"tool": "fetch_page", "source": url, "params": params, "url": url}
    url = _resolve(url)
    if not _public_host(url):
        return {**base, "rows": [], "total_rows": 0, "note": "Refused: only public http(s) URLs can be fetched."}
    try:
        # Redirects are followed by hand so every hop is checked as a public host.
        for _ in range(5):
            r = httpx.get(url, timeout=15, headers=UA, follow_redirects=False)
            if r.is_redirect and "location" in r.headers:
                url = str(r.url.join(r.headers["location"]))
                if not _public_host(url):
                    return {**base, "rows": [], "total_rows": 0, "note": "Refused: redirected to a non-public URL."}
                continue
            break
        r.raise_for_status()
    except httpx.HTTPError as e:
        return {**base, "rows": [], "total_rows": 0, "note": f"Could not fetch: {type(e).__name__}: {str(e)[:200]}"}
    parser = _Text()
    parser.feed(r.text)
    text = re.sub(r"\s+", " ", " ".join(parser.parts))
    return {**base, "url": url, "source": url,
            "rows": [{"title": parser.title.strip()[:200], "url": url, "text": text[:PAGE_CHARS]}], "total_rows": 1,
            "note": (f"Page text, cut to {PAGE_CHARS} characters." if len(text) > PAGE_CHARS else "Full page text.")
                    + " Statements here are the publisher's claims."}
