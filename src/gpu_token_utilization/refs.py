"""Reference checking for /deep-research (the verify_refs tool). A report may cite a paper only
with an id a registry resolves (DOI, arXiv id, OpenAlex id) or a URL that answers, and the title
and authors it gives must match what the registry says; anything else is dropped from the
report. Models invent plausible references; this is the step that makes that harmless.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from . import scholar
from .scholar import Paper, ScholarError, normalize_arxiv, normalize_doi, normalize_openalex, title_similarity

TITLE_MATCH = 0.85  # SequenceMatcher ratio at or above which two titles are the same paper
SOURCES_FILE = "sources.json"
PAGE_BYTES = 200_000


@dataclass
class Reference:
    key: str
    title: str = ""
    authors: list[str] | None = None
    year: int | None = None
    doi: str | None = None
    arxiv: str | None = None
    openalex: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, data: dict, index: int) -> Reference:
        authors = data.get("authors")
        if isinstance(authors, str):  # "A and B", "A; B", or "A, B, C" (but "Last, First" is one name)
            authors = [a.strip() for a in re.split(r";| and ", authors) if a.strip()]
            if len(authors) == 1 and authors[0].count(",") >= 2:
                authors = [a.strip() for a in authors[0].split(",") if a.strip()]
        return cls(
            key=str(data.get("key") or data.get("id") or f"ref{index}"),
            title=str(data.get("title") or ""),
            authors=list(authors) if authors else None,
            year=data.get("year") if isinstance(data.get("year"), int) else None,
            doi=normalize_doi(data.get("doi")) or normalize_doi(data.get("url")),
            arxiv=normalize_arxiv(data.get("arxiv")) or (normalize_arxiv(data.get("url")) if "arxiv.org" in str(data.get("url") or "") else None),
            openalex=normalize_openalex(data.get("openalex")),
            url=data.get("url"),
        )


@dataclass
class Verdict:
    key: str
    ok: bool
    reason: str
    found: Paper | None = None
    title_match: float | None = None

    def to_dict(self) -> dict:
        return {"key": self.key, "verified": self.ok, "verify_reason": self.reason,
                "title_match": None if self.title_match is None else round(self.title_match, 2),
                "resolved": self.found.to_dict() if self.found else None}


# --- matching ------------------------------------------------------------------------------------

def _last_name(name: str) -> str:
    """'Yann LeCun', 'LeCun, Y.' and 'Y. LeCun' all give 'lecun'."""
    if "," in name:
        name = name.split(",", 1)[0]
        parts = [p for p in re.sub(r"[^a-z\- ]", "", name.lower()).split() if p]
        return parts[-1] if parts else ""
    parts = [p for p in re.sub(r"[^a-z\- ]", "", name.lower()).split() if p]
    return parts[-1] if parts else ""


def authors_match(claimed: list[str] | None, found: list[str]) -> bool | None:
    """None when there's nothing to compare. True when the first claimed author's last name is
    among the registry's authors (any position: models often reorder), or half the claimed ones are."""
    if not claimed or not found:
        return None
    theirs = {_last_name(n) for n in found if _last_name(n)}
    ours = [_last_name(n) for n in claimed if _last_name(n)]
    if not ours or not theirs:
        return None
    if ours[0] in theirs:
        return True
    return sum(1 for n in ours if n in theirs) * 2 >= len(ours)


def compare(ref: Reference, found: Paper) -> Verdict:
    similarity = title_similarity(ref.title, found.title) if ref.title else None
    if similarity is not None and similarity < TITLE_MATCH:
        return Verdict(ref.key, False, f"the id resolves to a different title: {found.title!r}", found, similarity)
    authors = authors_match(ref.authors, found.authors)
    if authors is False:
        return Verdict(ref.key, False, f"the id resolves to other authors: {', '.join(found.authors[:4])}", found, similarity)
    if ref.year and found.year and abs(ref.year - found.year) > 1:
        return Verdict(ref.key, False, f"the year is {found.year}, not {ref.year}", found, similarity)
    return Verdict(ref.key, True, "resolves and matches", found, similarity)


# --- resolving -----------------------------------------------------------------------------------

def resolve(ref: Reference, http: httpx.Client) -> Paper | None:
    """The registry's record for the reference's strongest id: DOI (Crossref, then OpenAlex),
    then arXiv id, then OpenAlex id. None when no registry has it."""
    if ref.doi:
        try:
            found = scholar.crossref_work(ref.doi, http)
        except ScholarError:
            found = None
        if found is None:
            found = scholar.openalex_work(ref.doi, http)
        if found is not None:
            return found
    if ref.arxiv:
        found = scholar.arxiv_work(ref.arxiv, http)
        if found is not None:
            return found
    if ref.openalex:
        return scholar.openalex_work(ref.openalex, http)
    return None


def check_url(ref: Reference, http: httpx.Client) -> Verdict:
    """A reference with only a URL: the page must answer, and most of the title's words must be on it."""
    try:
        response = http.get(ref.url, headers={"Accept": "text/html,*/*"})
    except httpx.HTTPError as exc:
        return Verdict(ref.key, False, f"the URL couldn't be fetched ({type(exc).__name__})")
    if response.status_code != 200:
        return Verdict(ref.key, False, f"the URL answered HTTP {response.status_code}")
    if not ref.title:
        return Verdict(ref.key, True, "the URL answers (no title to check)")
    page = scholar.normalize_title(re.sub(r"<[^>]+>", " ", response.text[:PAGE_BYTES]))
    words = [w for w in scholar.normalize_title(ref.title).split() if len(w) > 3]
    if not words:
        return Verdict(ref.key, True, "the URL answers")
    hits = sum(1 for w in words if w in page)
    if hits * 10 < len(words) * 6:
        return Verdict(ref.key, False, f"the page doesn't carry the title ({hits} of {len(words)} title words found)")
    return Verdict(ref.key, True, "the URL answers and carries the title")


def verify(ref: Reference, http: httpx.Client) -> Verdict:
    if not (ref.doi or ref.arxiv or ref.openalex or ref.url):
        return Verdict(ref.key, False, "no DOI, arXiv id, OpenAlex id or URL to check")
    try:
        found = resolve(ref, http)
    except ScholarError as exc:
        return Verdict(ref.key, False, f"couldn't check it: {exc}")
    if found is not None:
        return compare(ref, found)
    if ref.url and not (ref.doi or ref.arxiv or ref.openalex):
        return check_url(ref, http)
    ids = ", ".join(f"{k} {v}" for k, v in (("DOI", ref.doi), ("arXiv", ref.arxiv), ("OpenAlex", ref.openalex)) if v)
    return Verdict(ref.key, False, f"no registry has {ids}")


# --- the sources file -----------------------------------------------------------------------------

def load_sources(path: Path) -> list[dict]:
    """sources.json: {"sources": [...]} or a bare list of reference objects."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    items = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ValueError(f"{path} must hold a list of references under \"sources\"")
    return items


def verify_file(folder: Path, http: httpx.Client | None = None) -> tuple[list[Verdict], Path]:
    """Check every reference in <folder>/sources.json and write the verdicts back into it
    ("verified", "verify_reason", "resolved"). Returns the verdicts and the file."""
    folder = Path(folder)
    path = folder / SOURCES_FILE if folder.is_dir() else folder
    items = load_sources(path)
    own = http is None
    http = http or scholar.client()
    try:
        verdicts = [verify(Reference.from_dict(item, i), http) for i, item in enumerate(items, 1)]
    finally:
        if own:
            http.close()
    for item, verdict in zip(items, verdicts):
        item.update(verdict.to_dict())
    path.write_text(json.dumps({"sources": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return verdicts, path


def describe(verdicts: list[Verdict], path: Path) -> str:
    kept = [v for v in verdicts if v.ok]
    dropped = [v for v in verdicts if not v.ok]
    lines = [f"Checked {len(verdicts)} reference(s) in {path}: {len(kept)} verified, {len(dropped)} to drop."]
    for v in dropped:
        lines.append(f"- DROP {v.key}: {v.reason}")
    for v in kept:
        where = v.found.url if v.found and v.found.url else ""
        lines.append(f"- keep {v.key}: {v.reason}" + (f" ({where})" if where else ""))
    if dropped:
        lines.append("Remove every DROP reference and the claims that rested on it from the report, or find a "
                     "source that resolves. Never keep a reference the registries don't have.")
    return "\n".join(lines)


def to_dicts(verdicts: list[Verdict]) -> list[dict]:
    return [asdict(v) if v.found is None else v.to_dict() for v in verdicts]
