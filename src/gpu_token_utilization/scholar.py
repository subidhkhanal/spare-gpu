"""Scholarly search for /deep-research (the scholar_search tool): OpenAlex, Crossref and arXiv,
three open registries whose APIs allow automated use (Google Scholar's doesn't, so it isn't
used). Results are normalised so a scout can cite a paper by an exact id (DOI, arXiv id,
OpenAlex id) rather than by memory, and refs.verify_refs can check the citation later.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from typing import Callable

import httpx

from . import __version__

USER_AGENT = f"spare-gpu-pack/{__version__} (+https://github.com/subidhkhanal/spare-gpu)"
OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
ARXIV = "https://export.arxiv.org/api/query"
SOURCES = ("openalex", "crossref", "arxiv")
ABSTRACT_CHARS = 500
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


class ScholarError(Exception):
    """A registry couldn't be reached or answered unexpectedly; the message says which."""


@dataclass
class Paper:
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    arxiv: str | None = None
    openalex: str | None = None
    url: str | None = None
    abstract: str | None = None
    cited_by: int | None = None
    source: str = ""

    def keys(self) -> set[str]:
        """Every id two results could share and so be the same paper: the DOI (an arXiv DOI counts
        as the arXiv id), the arXiv id, the OpenAlex id, and the normalised title."""
        found = set()
        if self.doi:
            found.add("arxiv:" + self.doi.split("arxiv.", 1)[1] if self.doi.startswith("10.48550/arxiv.") else "doi:" + self.doi)
        if self.arxiv:
            found.add(f"arxiv:{self.arxiv}")
        if self.openalex:
            found.add(f"openalex:{self.openalex}")
        if normalize_title(self.title):
            found.add("title:" + normalize_title(self.title))
        return found

    def to_dict(self) -> dict:
        return asdict(self)


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                        follow_redirects=True)


# --- ids and titles ------------------------------------------------------------------------------

def normalize_doi(text: str | None) -> str | None:
    """'https://doi.org/10.1038/Nature14539', 'doi:10.1038/...' and '10.1038/...' are one DOI."""
    if not text:
        return None
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", text.strip(), flags=re.I).strip().lower()
    return doi if re.match(r"^10\.\d{4,9}/\S+$", doi) else None


def normalize_arxiv(text: str | None) -> str | None:
    """'arXiv:1706.03762v5', 'https://arxiv.org/abs/1706.03762' and '1706.03762' are one paper."""
    if not text:
        return None
    found = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", text) or re.search(r"([a-z\-]+(\.[A-Z]{2})?/\d{7})(v\d+)?", text)
    return found.group(1) if found else None


def normalize_openalex(text: str | None) -> str | None:
    if not text:
        return None
    found = re.search(r"(W\d+)$", text.strip())
    return found.group(1) if found else None


def normalize_title(title: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (title or "").lower()).split())


def title_similarity(a: str | None, b: str | None) -> float:
    """0..1: how alike two titles are, ignoring case, punctuation and spacing."""
    x, y = normalize_title(a), normalize_title(b)
    if not x or not y:
        return 0.0
    return SequenceMatcher(None, x, y).ratio()


# --- OpenAlex ------------------------------------------------------------------------------------

def _openalex_paper(work: dict) -> Paper:
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    return Paper(
        title=work.get("title") or work.get("display_name") or "",
        authors=[(a.get("author") or {}).get("display_name") or "" for a in work.get("authorships") or []],
        year=work.get("publication_year"),
        venue=source.get("display_name"),
        doi=normalize_doi(work.get("doi")),
        arxiv=normalize_arxiv(location.get("landing_page_url") or "") if "arxiv" in (location.get("landing_page_url") or "") else None,
        openalex=normalize_openalex(work.get("id")),
        url=location.get("landing_page_url") or work.get("doi") or work.get("id"),
        abstract=_from_inverted(work.get("abstract_inverted_index")),
        cited_by=work.get("cited_by_count"),
        source="openalex",
    )


def _from_inverted(index: dict | None) -> str | None:
    """OpenAlex gives abstracts as {word: [positions]}; put the words back in order."""
    if not index:
        return None
    words = sorted((pos, word) for word, positions in index.items() for pos in positions)
    text = " ".join(word for _, word in words)
    return text[:ABSTRACT_CHARS] + ("…" if len(text) > ABSTRACT_CHARS else "")


OPENALEX_FIELDS = "id,doi,title,display_name,publication_year,authorships,primary_location,cited_by_count,abstract_inverted_index"


def search_openalex(query: str, limit: int, http: httpx.Client) -> list[Paper]:
    response = _get(http, f"{OPENALEX}/works", {"search": query, "per-page": limit, "select": OPENALEX_FIELDS}, "OpenAlex")
    return [_openalex_paper(w) for w in response.json().get("results") or []]


def openalex_work(ident: str, http: httpx.Client) -> Paper | None:
    """A work by DOI or OpenAlex id; None if the registry doesn't have it."""
    doi, wid = normalize_doi(ident), normalize_openalex(ident)
    if not doi and not wid:
        return None
    path = f"{OPENALEX}/works/{'https://doi.org/' + doi if doi else wid}"
    response = _get(http, path, {"select": OPENALEX_FIELDS}, "OpenAlex", missing_ok=True)
    return None if response.status_code == 404 else _openalex_paper(response.json())


# --- Crossref ------------------------------------------------------------------------------------

def _crossref_paper(item: dict) -> Paper:
    issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
    return Paper(
        title=(item.get("title") or [""])[0],
        authors=[" ".join(p for p in (a.get("given"), a.get("family")) if p) or a.get("name", "") for a in item.get("author") or []],
        year=issued[0] if issued else None,
        venue=(item.get("container-title") or [None])[0],
        doi=normalize_doi(item.get("DOI")),
        url=item.get("URL"),
        abstract=_strip_jats(item.get("abstract")),
        cited_by=item.get("is-referenced-by-count"),
        source="crossref",
    )


def _strip_jats(text: str | None) -> str | None:
    if not text:
        return None
    plain = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    return plain[:ABSTRACT_CHARS] + ("…" if len(plain) > ABSTRACT_CHARS else "")


CROSSREF_FIELDS = "DOI,title,author,issued,container-title,URL,abstract,is-referenced-by-count"


def search_crossref(query: str, limit: int, http: httpx.Client) -> list[Paper]:
    response = _get(http, f"{CROSSREF}/works", {"query": query, "rows": limit, "select": CROSSREF_FIELDS}, "Crossref")
    return [_crossref_paper(i) for i in ((response.json().get("message") or {}).get("items") or [])]


def crossref_work(doi: str, http: httpx.Client) -> Paper | None:
    doi = normalize_doi(doi)
    if not doi:
        return None
    response = _get(http, f"{CROSSREF}/works/{doi}", None, "Crossref", missing_ok=True)
    return None if response.status_code == 404 else _crossref_paper(response.json().get("message") or {})


# --- arXiv ---------------------------------------------------------------------------------------

def _arxiv_papers(xml_text: str) -> list[Paper]:
    try:
        feed = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ScholarError(f"arXiv answered with something that isn't a feed ({exc})") from None
    papers = []
    for entry in feed.findall(f"{ATOM}entry"):
        title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
        ident = normalize_arxiv(entry.findtext(f"{ATOM}id") or "")
        if title == "Error" or not ident:  # arXiv reports a malformed id as an entry titled Error
            continue
        published = entry.findtext(f"{ATOM}published") or ""
        papers.append(Paper(
            title=title,
            authors=[" ".join((a.findtext(f"{ATOM}name") or "").split()) for a in entry.findall(f"{ATOM}author")],
            year=int(published[:4]) if published[:4].isdigit() else None,
            venue=entry.findtext(f"{ARXIV_NS}journal_ref"),
            doi=normalize_doi(entry.findtext(f"{ARXIV_NS}doi")),
            arxiv=ident,
            url=f"https://arxiv.org/abs/{ident}",
            abstract=_clip(" ".join((entry.findtext(f"{ATOM}summary") or "").split())),
            source="arxiv",
        ))
    return papers


def _clip(text: str) -> str | None:
    return (text[:ABSTRACT_CHARS] + ("…" if len(text) > ABSTRACT_CHARS else "")) or None


def search_arxiv(query: str, limit: int, http: httpx.Client) -> list[Paper]:
    response = _get(http, ARXIV, {"search_query": f"all:{query}", "max_results": limit}, "arXiv")
    return _arxiv_papers(response.text)


def arxiv_work(arxiv_id: str, http: httpx.Client) -> Paper | None:
    ident = normalize_arxiv(arxiv_id)
    if not ident:
        return None
    response = _get(http, ARXIV, {"id_list": ident}, "arXiv")
    found = _arxiv_papers(response.text)
    return found[0] if found else None


# --- all together --------------------------------------------------------------------------------

def _get(http: httpx.Client, url: str, params: dict | None, who: str, missing_ok: bool = False) -> httpx.Response:
    try:
        response = http.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ScholarError(f"{who} couldn't be reached ({type(exc).__name__})") from None
    if response.status_code == 429:
        raise ScholarError(f"{who} asked us to slow down (HTTP 429); try again in a minute")
    if response.status_code != 200 and not (missing_ok and response.status_code == 404):
        raise ScholarError(f"{who} answered HTTP {response.status_code}")
    return response


SEARCHERS: dict[str, Callable[[str, int, httpx.Client], list[Paper]]] = {
    "openalex": search_openalex, "crossref": search_crossref, "arxiv": search_arxiv,
}


def search(query: str, limit: int = 5, sources: tuple[str, ...] = SOURCES,
           http: httpx.Client | None = None) -> tuple[list[Paper], list[str]]:
    """Up to `limit` papers from each registry, merged and deduplicated (most cited first within a
    source's order). Returns the papers and the notes about registries that didn't answer: one
    registry down never empties the result."""
    query = " ".join(query.split())
    if not query:
        raise ScholarError("no query given")
    own = http is None
    http = http or client()
    papers: list[Paper] = []
    seen: dict[str, Paper] = {}  # every id of every paper kept so far
    notes = []
    try:
        for name in sources:
            searcher = SEARCHERS.get(name)
            if searcher is None:
                notes.append(f"unknown source {name!r} (the sources are {', '.join(SOURCES)})")
                continue
            try:
                found = searcher(query, max(1, min(limit, 25)), http)
            except ScholarError as exc:
                notes.append(str(exc))
                continue
            for paper in found:
                same = next((seen[k] for k in paper.keys() if k in seen), None)
                if same is not None:
                    _fill_in(same, paper)
                    paper = same
                else:
                    papers.append(paper)
                for k in paper.keys():
                    seen.setdefault(k, paper)
    finally:
        if own:
            http.close()
    return papers, notes


def _fill_in(kept: Paper, other: Paper) -> None:
    """The same paper from a second registry: keep the first, add the ids and fields it lacked."""
    for name in ("doi", "arxiv", "openalex", "venue", "abstract", "year", "cited_by", "url"):
        if getattr(kept, name) in (None, "", []) and getattr(other, name) not in (None, "", []):
            setattr(kept, name, getattr(other, name))
    if not kept.authors and other.authors:
        kept.authors = other.authors


def describe(papers: list[Paper], notes: list[str]) -> str:
    """The results as text for an agent: one block per paper, ids first, so they get quoted exactly."""
    if not papers:
        return "No papers found." + ("".join(f"\n- {n}" for n in notes) if notes else "")
    lines = []
    for i, p in enumerate(papers, 1):
        ids = ", ".join(f"{k}: {v}" for k, v in (("doi", p.doi), ("arxiv", p.arxiv), ("openalex", p.openalex)) if v)
        who = ", ".join(a for a in p.authors[:6] if a) + (" et al." if len(p.authors) > 6 else "")
        lines.append(f"{i}. {p.title} ({p.year or 'n.d.'}) — {who or 'authors unknown'}"
                     + (f". {p.venue}" if p.venue else "")
                     + (f". Cited by {p.cited_by}" if p.cited_by is not None else "")
                     + f"\n   ids: {ids or 'none'}; url: {p.url or 'none'}; found in: {p.source}"
                     + (f"\n   abstract: {p.abstract}" if p.abstract else ""))
    if notes:
        lines.append("Notes: " + "; ".join(notes))
    return "\n".join(lines)
