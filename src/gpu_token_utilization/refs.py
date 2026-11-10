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
from urllib.parse import parse_qsl, urlencode, urlsplit

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


# --- the scouts' sources, merged into one file ------------------------------------------------------

NOTES_DIR = "notes"
# A scout's machine-readable list: a ```sources fence with one JSON object per line.
_SOURCES_FENCE = re.compile(r"```sources[ \t]*\r?\n(.*?)```", re.S)
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


_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$")


def identities(item: dict) -> list[str]:
    """Every id an entry gives (DOI, arXiv, OpenAlex, URL), each as "label:value". Two entries
    sharing any one of them are the same source. arXiv's own DOI (10.48550/arXiv.<id>) counts as
    the arXiv id, so the same preprint cited by its DOI and by its id still matches."""
    found: list[str] = []

    def add(label: str, value) -> None:
        if value and f"{label}:{str(value).lower()}" not in found:
            found.append(f"{label}:{str(value).lower()}")

    url = str(item.get("url") or "")
    for doi in (normalize_doi(item.get("doi")), normalize_doi(item.get("url"))):
        arxiv_doi = _ARXIV_DOI.match(doi or "")
        if arxiv_doi:
            add("arxiv", normalize_arxiv(arxiv_doi.group(1)))
        else:
            add("doi", doi)
    add("arxiv", normalize_arxiv(item.get("arxiv")))
    if "arxiv.org" in url:
        add("arxiv", normalize_arxiv(url))
    add("openalex", normalize_openalex(item.get("openalex")))
    add("url", normalize_url(item.get("url")))
    return found


def identity(item: dict) -> str | None:
    """The entry's first id (DOI, then arXiv, OpenAlex, URL), or None when it gives none."""
    ids = identities(item)
    return ids[0] if ids else None


def _note_sources(text: str) -> tuple[list[dict], int]:
    """The source objects in a notes file's ```sources fences, and how many lines didn't parse."""
    found, bad = [], 0
    for block in _SOURCES_FENCE.findall(text):
        for line in block.splitlines():
            line = line.strip().rstrip(",")
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(item, dict) and item.get("key"):
                found.append(item)
            else:
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
    found by several scouts (same DOI, arXiv id, OpenAlex id or URL) becomes one entry, its other
    keys kept as "aliases" so citations under any of them still work; two different sources a
    scout gave the same key get the later one renamed, in that notes file too. A source with no id
    is left out: a fact needs one. Returns the file and a summary, with the quality mix."""
    folder = Path(folder)
    notes = sorted((folder / NOTES_DIR).glob("*.md"), key=_section_order)
    if not notes:
        raise FileNotFoundError(folder / NOTES_DIR)
    merged: list[dict] = []
    by_identity: dict[str, dict] = {}
    by_key: dict[str, dict] = {}
    seen = duplicates = bad = 0
    no_id: list[str] = []
    renamed: list[str] = []
    for path in notes:
        items, unparsed = _note_sources(path.read_text(encoding="utf-8-sig"))
        bad += unparsed
        for item in items:
            seen += 1
            key = str(item["key"])
            idents = identities(item)
            if not idents:
                no_id.append(f"{key} ({path.name})")
                continue
            kept = next((by_identity[i] for i in idents if i in by_identity), None)
            if kept is not None:
                duplicates += 1
                for ident in idents:  # the ids only this entry gave now find the kept source too
                    by_identity.setdefault(ident, kept)
                if key != kept["key"] and key not in kept.setdefault("aliases", []):
                    if key in by_key and by_key[key] is not kept:  # the key already names another source
                        new = _free_key(key, by_key)
                        _rename_key(path, key, new)
                        renamed.append(f"{key} -> {new} in {path.name}")
                        key = new
                    kept["aliases"].append(key)
                    by_key[key] = kept
                for field in ("quality", "date", "title", "authors", "year", "doi", "arxiv", "openalex", "url"):
                    # fill what the first scout left out
                    if not kept.get(field) and item.get(field):
                        kept[field] = item[field]
                continue
            if key in by_key:  # a key already used for a different source
                new = _free_key(key, by_key)
                _rename_key(path, key, new)
                renamed.append(f"{key} -> {new} in {path.name}")
                item = {**item, "key": new}
                key = new
            entry = {k: v for k, v in item.items() if k != "aliases"}
            merged.append(entry)
            for ident in idents:
                by_identity.setdefault(ident, entry)
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
        lines.append(f"{bad} line(s) in ```sources fences weren't a JSON object with a key, and were skipped.")
    return out, "\n".join(lines)


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
    parts = [f"[{item['key']}]", head, f"*{item.get('title') or 'untitled'}*." if item.get("title") else "", ids]
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
