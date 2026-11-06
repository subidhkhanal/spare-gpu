"""Record real answers from OpenAlex, Crossref and arXiv into tests/fixtures/scholar/.

The tests run against those files, never the network. Run this on a machine with internet
access to replace the hand-written fixtures with recordings (then look at the test assertions:
they name the papers the queries are expected to find).

    python scripts/record_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from gpu_token_utilization import scholar  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "scholar"
QUERY = "attention is all you need transformer"
DOI, MISSING_DOI, ARXIV = "10.1038/nature14539", "10.1000/spare-gpu-no-such-doi", "1706.03762"

REQUESTS = {
    "openalex-search.json": (f"{scholar.OPENALEX}/works", {"search": QUERY, "per-page": 5, "select": scholar.OPENALEX_FIELDS}),
    "openalex-doi.json": (f"{scholar.OPENALEX}/works/https://doi.org/{DOI}", {"select": scholar.OPENALEX_FIELDS}),
    "openalex-missing.json": (f"{scholar.OPENALEX}/works/https://doi.org/{MISSING_DOI}", None),
    "crossref-search.json": (f"{scholar.CROSSREF}/works", {"query": QUERY, "rows": 5, "select": scholar.CROSSREF_FIELDS}),
    "crossref-doi.json": (f"{scholar.CROSSREF}/works/{DOI}", None),
    "crossref-missing.txt": (f"{scholar.CROSSREF}/works/{MISSING_DOI}", None),
    "arxiv-search.xml": (scholar.ARXIV, {"search_query": f"all:{QUERY}", "max_results": 5}),
    "arxiv-id.xml": (scholar.ARXIV, {"id_list": ARXIV}),
    "arxiv-missing.xml": (scholar.ARXIV, {"id_list": "9999.99999"}),
    "arxiv-bad-id.xml": (scholar.ARXIV, {"id_list": "nope"}),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with scholar.client() as http:
        for name, (url, params) in REQUESTS.items():
            response = http.get(url, params=params)
            (OUT / name).write_text(response.text, encoding="utf-8")
            print(f"{response.status_code}  {name}  {len(response.text):,} bytes")
    print(f"Recorded into {OUT}. Run pytest tests/test_scholar.py tests/test_refs.py.")


if __name__ == "__main__":
    main()
