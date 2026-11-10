"""Reference checking for /deep-research (the verify_refs tool). A report may cite a paper only
with an id a registry resolves (DOI, arXiv id, OpenAlex id) or a URL that answers, and the title
and authors it gives must match what the registry says; anything else is dropped from the
report. Models invent plausible references; this is the step that makes that harmless. A source
that couldn't be reached (a refusing or busy site) is "unchecked", never dropped: that says
nothing against it. A real paper given with a shortened title or the wrong authors is kept, its
details corrected from the registry.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from . import scholar
from .scholar import Paper, ScholarError, normalize_arxiv, normalize_doi, normalize_openalex, title_similarity

TITLE_MATCH = 0.85  # SequenceMatcher ratio at or above which two titles are the same paper
TITLE_EXACT = 0.95  # the same title: a resolving id with this title is the paper, whatever authors were written
TITLE_SHORTENED = 0.75  # a shortened title is still the paper when the authors match too
# Answers that say nothing about whether the source exists: refused, rate-limited or a server fault.
UNCHECKABLE_STATUS = {401, 403, 405, 408, 429, 451} | set(range(500, 600))
_NO_AUTHOR = re.compile(r"^\W*(unlisted|unknown|anonymous|anon|n/?a|none|various)\b", re.I)
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
        authors = data.get("authors") or data.get("author")
        if isinstance(authors, list):
            authors = [str(a) for a in authors if str(a).strip() and not _NO_AUTHOR.match(str(a))]
        elif isinstance(authors, str) and _NO_AUTHOR.match(authors):
            authors = None
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
    checked: bool = True  # False: the source couldn't be reached, so it's neither kept nor dropped
    corrected: dict | None = None  # fields the registry corrected (authors, title), written back into sources.json

    @property
    def status(self) -> str:
        return "keep" if self.ok else ("drop" if self.checked else "unchecked")

    def to_dict(self) -> dict:
        return {"key": self.key, "verified": self.ok if self.checked else None, "verify_reason": self.reason,
                "title_match": None if self.title_match is None else round(self.title_match, 2),
                "resolved": self.found.to_dict() if self.found else None}


# --- matching ------------------------------------------------------------------------------------

def _last_name(name: str) -> str:
    """'Yann LeCun', 'LeCun, Y.', 'Y. LeCun' and 'LeCun et al. 2015' all give 'lecun'."""
    name = re.sub(r"\b(et\.?\s*al\b\.?|and others|others)(.*)$", "", name, flags=re.I).strip()
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
    authors = authors_match(ref.authors, found.authors)
    if similarity is not None and similarity < TITLE_MATCH:
        if similarity >= TITLE_SHORTENED and authors is True:  # a shortened title, by the right authors
            return Verdict(ref.key, True, "resolves; the title was shortened and is corrected from the registry",
                           found, similarity, corrected={"title": found.title})
        return Verdict(ref.key, False, f"the id resolves to a different title: {found.title!r}", found, similarity)
    if authors is False:
        if similarity is not None and similarity >= TITLE_EXACT:  # the paper is real; the author list was wrong
            return Verdict(ref.key, True, "resolves and the title matches; the authors were wrong and are corrected "
                           f"from the registry: {', '.join(found.authors[:4])}", found, similarity,
                           corrected={"authors": list(found.authors)})
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
        return Verdict(ref.key, False, f"couldn't check it: the URL couldn't be fetched ({type(exc).__name__})",
                       checked=False)
    if response.status_code in UNCHECKABLE_STATUS:
        return Verdict(ref.key, False, f"couldn't check it: the site answered HTTP {response.status_code} "
                       "(refused or busy), which says nothing about whether the page exists", checked=False)
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
        return Verdict(ref.key, False, f"couldn't check it: {exc}", checked=False)
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
        item.update(verdict.corrected or {})
        item.update(verdict.to_dict())
    path.write_text(json.dumps({"sources": items}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return verdicts, path


def describe(verdicts: list[Verdict], path: Path) -> str:
    kept = [v for v in verdicts if v.status == "keep"]
    dropped = [v for v in verdicts if v.status == "drop"]
    unchecked = [v for v in verdicts if v.status == "unchecked"]
    lines = [f"Checked {len(verdicts)} reference(s) in {path}: {len(kept)} verified, {len(dropped)} to drop"
             + (f", {len(unchecked)} couldn't be checked." if unchecked else ".")]
    for v in dropped:
        lines.append(f"- DROP {v.key}: {v.reason}")
    for v in unchecked:
        lines.append(f"- UNCHECKED {v.key}: {v.reason}")
    for v in kept:
        where = v.found.url if v.found and v.found.url else ""
        lines.append(f"- keep {v.key}: {v.reason}" + (f" ({where})" if where else ""))
    if dropped:
        lines.append("Remove every DROP reference and the claims that rested on it from the report, or find a "
                     "source that resolves. Never keep a reference the registries don't have.")
    if unchecked:
        lines.append("UNCHECKED references are not DROP: the source couldn't be reached, so nothing is known against "
                     "it. Keep them; the report lists them as not checked automatically. Run verify_refs again "
                     "later to settle them.")
    return "\n".join(lines)


# --- the scouts' sources, merged into one file ------------------------------------------------------

NOTES_DIR = "notes"
# A scout's machine-readable list: a ```sources fence with one JSON object per line.
_SOURCES_FENCE = re.compile(r"```sources?[ \t]*\r?\n(.*?)```", re.S | re.I)
# A plain fence right under a "Sources" heading: scouts sometimes write the list that way.
_HEADED_FENCE = re.compile(r"^#{1,6}[ \t]*(?:sources|references)\b[^\n]*\n\s*```[\w-]*[ \t]*\r?\n(.*?)```",
                           re.S | re.I | re.M)
_ID_FIELDS = ("id", "ids", "identifier", "link", "source", "ref")
_AUTHOR_FIELDS = ("authors", "author", "author/year", "authors/year", "by")
_QUALITY_FIELDS = ("quality", "rating", "grade", "rank")
_KEY = re.compile(r"^[\w.:\-]{2,60}$")
_KINDS = {"paper", "web", "official", "repo", "docs", "blog", "news", "book", "report", "dataset", "preprint"}
QUALITY = {"A": "primary or authoritative", "B": "reputable secondary", "C": "weak (blog, forum, vendor page)"}
_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|mc_\w+|ref|ref_src)$", re.I)


def normalize_url(url: str | None) -> str | None:
    """A URL reduced to what names the page: no scheme, no www., no fragment, no tracking
    parameters, no trailing slash, host in lower case. None for anything that isn't a web URL."""
    if not url or not isinstance(url, str):
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/")
    return host + path + (f"?{query}" if query else "")


_ARXIV_DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)?10\.48550/arxiv\.(.+)$", re.I)
ID_FIELDS = ("doi", "arxiv", "openalex", "url")


def _arxiv_from_doi(text) -> str | None:
    """The arXiv id inside arXiv's own DOI (10.48550/arXiv.<id>), its case kept for old-style ids."""
    found = _ARXIV_DOI.match(str(text or "").strip())
    return normalize_arxiv(found.group(1)) if found else None


def strong_ids(item: dict) -> list[str]:
    """The entry's paper ids (DOI, arXiv, OpenAlex), each as "label:value". arXiv's own DOI counts
    as the arXiv id, so a preprint cited by its DOI and by its id matches."""
    found: list[str] = []

    def add(label: str, value) -> None:
        if value and f"{label}:{str(value).lower()}" not in found:
            found.append(f"{label}:{str(value).lower()}")

    url = item.get("url")
    for raw in (item.get("doi"), url):
        arxiv = _arxiv_from_doi(raw)
        add("arxiv", arxiv) if arxiv else add("doi", normalize_doi(raw))
    add("arxiv", normalize_arxiv(item.get("arxiv")))
    if "arxiv.org" in str(url or ""):
        add("arxiv", normalize_arxiv(url))
    add("openalex", normalize_openalex(item.get("openalex")))
    return found


def url_id(item: dict) -> str | None:
    """The entry's page, as "url:<normalised url>"."""
    url = normalize_url(item.get("url"))
    return f"url:{url.lower()}" if url else None


def identities(item: dict) -> list[str]:
    """What makes two entries the same source: the paper ids when it has any, else its page. A page
    alone never joins two entries with paper ids: different papers can share a page (a proceedings
    volume, a listing), so only an entry with nothing better is matched by it."""
    return strong_ids(item) or [i for i in (url_id(item),) if i]


def identity(item: dict) -> str | None:
    """The entry's first id (DOI, then arXiv, OpenAlex, URL), or None when it gives none."""
    ids = identities(item)
    return ids[0] if ids else None


def ids_in(text: str) -> dict:
    """The DOI, arXiv id, OpenAlex id and URL written anywhere in a piece of text."""
    text = str(text or "")
    found: dict = {}
    doi = re.search(r"\b10\.\d{4,9}/[^\s|)\]>,;\"']+", text)
    if doi:
        found["doi"] = doi.group(0).rstrip(".")
    arxiv = (re.search(r"arxiv(?:\.org)?[:\s/]*(?:abs/|pdf/|html/)?(\d{4}\.\d{4,5}(?:v\d+)?)", text, re.I)
             or re.fullmatch(r"\s*(\d{4}\.\d{4,5}(?:v\d+)?)\s*", text))
    if arxiv:
        found["arxiv"] = arxiv.group(1)
    url = re.search(r"https?://[^\s|)\]>\"']+", text)
    if url:
        found["url"] = url.group(0).rstrip(".,;")
    openalex = re.search(r"\b(W\d{5,})\b", text)
    if openalex:
        found["openalex"] = openalex.group(1)
    return found


def _pipe_entry(line: str) -> dict | None:
    """`key | title | author/year | type | id | rating`, or any order after the key and title."""
    parts = [p.strip() for p in line.strip().strip("-* ").strip("|").split("|")]
    if len(parts) < 3 or not _KEY.match(parts[0]) or parts[0].lower() in ("key", "id"):
        return None
    item: dict = {"key": parts[0], "title": parts[1]}
    for part in parts[2:]:
        ids = ids_in(part)
        if re.fullmatch(r"[ABC]", part, re.I):
            item["quality"] = part.upper()
        elif ids:
            for name, value in ids.items():
                item.setdefault(name, value)
        elif part.lower() in _KINDS:
            item["type"] = part.lower()
        elif part and "authors" not in item:
            item["authors"] = part
    return item


def normalize_entry(item: dict) -> dict:
    """One scout's source entry in the shape the rest expects: ids in doi/arxiv/openalex/url, the
    rating in quality, the authors in authors and the year as a number, whatever names it used."""
    item = dict(item)
    for name in _ID_FIELDS:
        value = item.get(name)
        if name != "id" or not _KEY.match(str(value or "")) or ids_in(str(value)):  # "id" may be the key itself
            for found, text in ids_in(" ".join(value) if isinstance(value, list) else str(value or "")).items():
                item.setdefault(found, text)
    for name in ("doi", "arxiv", "url", "openalex"):  # an id written in the wrong field ("doi": "arXiv:2404.07060")
        if isinstance(item.get(name), str) and name == "doi" and not normalize_doi(item[name]):
            moved = ids_in(item.pop(name))
            for found, text in moved.items():
                item.setdefault(found, text)
    if not item.get("quality"):
        for name in _QUALITY_FIELDS[1:]:
            grade = re.match(r"\s*([ABC])\b", str(item.get(name) or ""), re.I)
            if grade:
                item["quality"] = grade.group(1).upper()
                break
    elif isinstance(item["quality"], str):
        item["quality"] = item["quality"].strip()[:1].upper()
    if not item.get("authors"):
        for name in _AUTHOR_FIELDS[1:]:
            if item.get(name):
                item["authors"] = item[name]
                break
    acl = re.search(r"aclanthology\.org/(\d{4}\.[a-z\-]+\.\d+)(?:\.pdf)?/?$", str(item.get("url") or ""), re.I)
    if acl and not item.get("doi"):  # an ACL Anthology page's id is its DOI's suffix
        item["doi"] = f"10.18653/v1/{acl.group(1)}"
    if isinstance(item.get("authors"), str):  # "Lewis, Perez et al. / 2020": the year goes in year
        year = re.search(r"\s*[/,(]?\s*((?:19|20)\d{2})\)?\s*$", item["authors"])
        if year:
            item.setdefault("year", int(year.group(1)))
            item["authors"] = item["authors"][:year.start()].strip() or item["authors"]
    if not item.get("year"):
        year = re.search(r"\b(19|20)\d{2}\b", str(item.get("authors") or "") + " " + str(item.get("date") or ""))
        if year:
            item["year"] = int(year.group(0))
    elif isinstance(item["year"], str) and item["year"].strip().isdigit():
        item["year"] = int(item["year"].strip())
    return item


def _note_sources(text: str) -> tuple[list[dict], int]:
    """The source entries in a notes file's sources lists (a ```sources fence, or a fence right under
    a "Sources" heading), as JSON objects or `key | title | ... | id | rating` lines; and how many
    lines in those lists couldn't be read as a source."""
    blocks = [m for m in _SOURCES_FENCE.finditer(text)]
    spans = [m.span() for m in blocks]
    blocks += [m for m in _HEADED_FENCE.finditer(text)
               if not any(a <= m.start(1) < b for a, b in spans)]
    found, bad = [], 0
    for block in blocks:
        for line in block.group(1).splitlines():
            line = line.strip().rstrip(",")
            if not line or re.fullmatch(r"[|\-:\s]+", line):
                continue
            item = None
            if line.startswith("{"):
                try:
                    item = json.loads(line)
                except ValueError:
                    item = None
                if isinstance(item, dict) and not item.get("key") and isinstance(item.get("id"), str) \
                        and _KEY.match(item["id"]) and not ids_in(item["id"]):
                    item["key"] = item["id"]  # some scouts call the key "id"
            elif "|" in line:
                item = _pipe_entry(line)
            if isinstance(item, dict) and item.get("key"):
                found.append(normalize_entry(item))
            elif not re.match(r"^\|?\s*key\s*\|", line, re.I):  # a table's header row isn't a bad line
                bad += 1
    return found, bad


def _rename_key(path: Path, old: str, new: str) -> None:
    """Give a source another key in one notes file: in its sources fence and every [citation]."""
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r'("key"\s*:\s*)"' + re.escape(old) + '"', lambda m: m.group(1) + json.dumps(new), text)

    def cite(match: re.Match) -> str:
        keys = re.split(r"(\s*[,;]\s*)", match.group(1))
        return "[" + "".join(new if k == old else k for k in keys) + "]"

    text = _CITATION.sub(cite, text)
    path.write_text(text, encoding="utf-8")


def collect_sources(folder: Path) -> tuple[Path, str]:
    """Merge the ```sources lists of <folder>/notes/*.md into <folder>/sources.json. The same source
    found by several scouts (sharing a DOI, arXiv id or OpenAlex id; or the same URL, when one of
    the two has no paper id) becomes one entry, its other keys kept as "aliases" so citations under
    any of them still work; an entry that shares ids with two kept sources joins them into one. Two
    different sources a scout gave the same key get the later one renamed, in that notes file too.
    A source with no id is left out: a fact needs one. Returns the file and a summary, with the
    quality mix."""
    folder = Path(folder)
    notes = sorted((folder / NOTES_DIR).glob("*.md"), key=_section_order)
    if not notes:
        raise FileNotFoundError(folder / NOTES_DIR)
    merged: list[dict] = []
    by_identity: dict[str, dict] = {}  # paper id -> its source
    by_page: dict[str, dict] = {}  # page -> the first source seen there
    by_key: dict[str, dict] = {}
    seen = duplicates = bad = 0
    unreadable: list[str] = []
    no_list: list[str] = []
    no_id: list[str] = []
    renamed: list[str] = []
    for path in notes:
        items, unparsed = _note_sources(path.read_text(encoding="utf-8-sig"))
        bad += unparsed
        if unparsed:
            unreadable.append(f"{path.name} ({unparsed})")
        if not items and not unparsed:
            no_list.append(path.name)
        for item in items:
            seen += 1
            key = str(item["key"])
            strong = strong_ids(item)
            page = url_id(item)
            if not strong and not page:
                no_id.append(f"{key} ({path.name})")
                continue
            # every source that shares a paper id with this entry is one source from now on, the
            # earliest first; a page alone matches only a source known by nothing more than its page
            found = list({id(e): e for e in (by_identity[i] for i in strong if i in by_identity)}.values())
            if not found and page in by_page and (not strong or not strong_ids(by_page[page])):
                found = [by_page[page]]
            found.sort(key=lambda e: next(n for n, m in enumerate(merged) if m is e))
            if found:
                kept = found[0]
                duplicates += 1
                for other in found[1:]:  # this entry links two sources the earlier notes kept apart
                    _absorb(kept, other, by_identity, by_page, by_key)
                    merged.remove(other)
                    duplicates += 1
                if key != kept["key"] and key not in kept.setdefault("aliases", []):
                    if key in by_key and by_key[key] is not kept:  # the key already names another source
                        new = _free_key(key, by_key)
                        _rename_key(path, key, new)
                        renamed.append(f"{key} -> {new} in {path.name}")
                        key = new
                    kept["aliases"].append(key)
                    by_key[key] = kept
                _fill(kept, item, ids=bool(strong))
                for ident in strong_ids(kept):
                    by_identity[ident] = kept
                if page and page not in by_page:
                    by_page[page] = kept
                continue
            if key in by_key:  # a key already used for a different source
                new = _free_key(key, by_key)
                _rename_key(path, key, new)
                renamed.append(f"{key} -> {new} in {path.name}")
                item = {**item, "key": new}
                key = new
            entry = {k: v for k, v in item.items() if k != "aliases"}
            merged.append(entry)
            for ident in strong:
                by_identity[ident] = entry
            if page and page not in by_page:
                by_page[page] = entry
            by_key[key] = entry
    out = folder / SOURCES_FILE
    out.write_text(json.dumps({"sources": merged}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    mix = {grade: sum(1 for e in merged if str(e.get("quality") or "").upper() == grade) for grade in QUALITY}
    unrated = len(merged) - sum(mix.values())
    lines = [f"Wrote {out}: {len(merged)} source(s) from {seen} entr(ies) in {len(notes)} notes file(s); "
             f"{duplicates} duplicate(s) merged."]
    lines.append("Quality: " + ", ".join(f"{n} {grade} ({QUALITY[grade]})" for grade, n in mix.items())
                 + (f", {unrated} unrated" if unrated else "") + ".")
    if renamed:
        lines.append("Keys renamed because two different sources shared them (the notes were updated): "
                     + "; ".join(renamed) + ".")
    if no_id:
        lines.append(f"Left out, no DOI, arXiv id, OpenAlex id or URL: {', '.join(no_id)}.")
    if bad:
        lines.append(f"{bad} line(s) in the sources lists couldn't be read as a source and were skipped: "
                     f"{', '.join(unreadable)}. Each needs a key and an id (DOI, arXiv id, OpenAlex id or URL).")
    if no_list:
        lines.append(f"No sources list found in: {', '.join(no_list)}. A scout's notes end with a ```sources fence.")
    lines.append("This file is rebuilt from the notes on every call: fix a notes file and call collect_sources "
                 "again, never edit sources.json by hand.")
    return out, "\n".join(lines)


def _fill(kept: dict, item: dict, ids: bool) -> None:
    """Fill what the kept entry left out from a duplicate of it. Ids are copied only from an entry
    with paper ids, which matched on one or joined a source known only by its page; so a page shared
    by different papers never lends one paper's DOI to another."""
    for field in ("quality", "date", "title", "authors", "year", *(ID_FIELDS if ids else ())):
        if not kept.get(field) and item.get(field):
            kept[field] = item[field]


def _absorb(kept: dict, other: dict, by_identity: dict, by_page: dict, by_key: dict) -> None:
    """Fold a source kept earlier into another, when a later entry shows the two are one paper."""
    for alias in [other["key"], *other.get("aliases", [])]:
        if alias != kept["key"] and alias not in kept.setdefault("aliases", []):
            kept["aliases"].append(alias)
        by_key[alias] = kept
    _fill(kept, other, ids=True)
    for index in (by_identity, by_page):
        for ident, entry in list(index.items()):
            if entry is other:
                index[ident] = kept
    for ident in strong_ids(kept):
        by_identity[ident] = kept


def _free_key(key: str, used: dict) -> str:
    n = 2
    while f"{key}-{n}" in used:
        n += 1
    return f"{key}-{n}"


# --- the report, put together from its section files ------------------------------------------------

SECTIONS_DIR = "sections"
REPORT_FILE = "report.md"
# [key], [a, b] or [a; b]; not a Markdown link's text ([text](url)).
_CITATION = re.compile(r"\[([A-Za-z][\w.:-]*(?:\s*[,;]\s*[A-Za-z][\w.:-]*)*)\](?!\()")


def cited_keys(text: str) -> list[str]:
    """The source keys a text cites, in order of first citation."""
    keys: list[str] = []
    for match in _CITATION.finditer(text):
        for key in re.split(r"\s*[,;]\s*", match.group(1)):
            if key not in keys:
                keys.append(key)
    return keys


def _section_order(path: Path):
    number = re.match(r"\d+", path.name)
    return (int(number.group()) if number else 10**9, path.name)


def _as_section(text: str) -> str:
    """A section file's text with its headings below the report's title: a writer that starts with
    `# Abstract` instead of `## Abstract` gets every heading moved down one level (code blocks
    untouched), so report.md keeps one top-level title."""
    lines = text.strip().splitlines()
    first = next((line for line in lines if line.strip()), "")
    if not re.match(r"#\s", first):
        return text.strip()
    fenced = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and re.match(r"#{1,5}\s", line):
            lines[i] = "#" + line
    return "\n".join(lines)


def _reference_line(item: dict) -> str:
    authors = item.get("authors")
    if isinstance(authors, list):
        authors = ", ".join(str(a) for a in authors)
    head = " ".join(p for p in (str(authors or "").strip(), f"({item['year']})." if item.get("year") else "") if p)
    ids = "; ".join(f"{label} {item[name]}" for name, label in (("doi", "DOI"), ("arxiv", "arXiv"),
                                                              ("openalex", "OpenAlex"), ("url", "URL")) if item.get(name))
    unchecked = ("(not checked automatically: the source couldn't be reached)"
                 if item.get("verified") is None and item.get("verify_reason") else "")
    parts = [f"[{item['key']}]", head, f"*{item.get('title') or 'untitled'}*." if item.get("title") else "", ids, unchecked]
    return "- " + " ".join(p for p in parts if p)


def build_report(folder: Path, title: str = "") -> tuple[Path, str]:
    """Join <folder>/sections/*.md (in number order) into <folder>/report.md under `title`, then add
    the References section from sources.json: every cited key, in key order, except those
    verify_refs marked to drop. Returns the report and a summary that names what is missing:
    cited keys sources.json lacks or dropped, so their sentences can be fixed in the sections."""
    folder = Path(folder)
    sections = sorted((folder / SECTIONS_DIR).glob("*.md"), key=_section_order)
    if not sections:
        raise FileNotFoundError(folder / SECTIONS_DIR)
    body = "\n\n".join(_as_section(s.read_text(encoding="utf-8-sig")) for s in sections)
    try:
        sources = load_sources(folder / SOURCES_FILE)
    except FileNotFoundError:
        sources = []
    items = {str(i.get("key")): i for i in sources}
    alias = {str(a): str(i.get("key")) for i in sources for a in (i.get("aliases") or [])}
    if alias:  # cite every source by one key: the one its entry has
        body = _CITATION.sub(lambda m: "[" + "".join(alias.get(k, k) for k in re.split(r"(\s*[,;]\s*)", m.group(1))) + "]",
                             body)
    cited = cited_keys(body)
    unknown = [k for k in cited if k not in items]
    dropped = [k for k in cited if k in items and items[k].get("verified") is False]
    listed = sorted((k for k in cited if k in items and k not in dropped), key=str.lower)
    lines = [f"# {title.strip() or 'Research report'}", "", body, "", "## References", ""]
    lines += [_reference_line(items[k]) for k in listed] or ["(none cited)"]
    report = folder / REPORT_FILE
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary = [f"Wrote {report} from {len(sections)} section file(s): {', '.join(s.name for s in sections)}; "
               f"{len(listed)} reference(s) listed."]
    if dropped:
        summary.append(f"Cited but marked DROP by verify_refs, left out of References: {', '.join(dropped)}. "
                       "Remove the sentences that rest on them from the section files, then build again.")
    if unknown:
        summary.append(f"Cited but not in {SOURCES_FILE}: {', '.join(unknown)}. Fix those citations in the "
                       "section files, then build again.")
    return report, "\n".join(summary)


def to_dicts(verdicts: list[Verdict]) -> list[dict]:
    return [asdict(v) if v.found is None else v.to_dict() for v in verdicts]
