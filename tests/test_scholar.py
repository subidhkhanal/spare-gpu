"""scholar_search's registries against recorded answers (tests/fixtures/scholar/): results are
normalised with exact ids, the same paper from two registries is merged, a registry that is
down doesn't empty the result, and ids in their many spellings are recognised."""

from pathlib import Path

import httpx
import pytest

from gpu_token_utilization import scholar
from gpu_token_utilization.scholar import ScholarError

FIXTURES = Path(__file__).parent / "fixtures" / "scholar"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def registries(down=(), log=None):
    """A transport that answers like the three registries from the recorded files."""
    def handle(request):
        url, host = str(request.url), request.url.host
        if log is not None:
            log.append(url)
        if any(d in host for d in down):
            return httpx.Response(503)
        if host == "api.openalex.org":
            if "/works/" in request.url.path:
                if "10.1038/nature14539" in url or "W2250539671" in request.url.path:
                    return httpx.Response(200, text=fixture("openalex-doi.json"))
                return httpx.Response(404, text=fixture("openalex-missing.json"))
            return httpx.Response(200, text=fixture("openalex-search.json"))
        if host == "api.crossref.org":
            if "/works/" in request.url.path:
                if "10.1038/nature14539" in request.url.path:
                    return httpx.Response(200, text=fixture("crossref-doi.json"))
                return httpx.Response(404, text=fixture("crossref-missing.txt"))
            return httpx.Response(200, text=fixture("crossref-search.json"))
        if host == "export.arxiv.org":
            ids = request.url.params.get("id_list")
            if ids == "1706.03762":
                return httpx.Response(200, text=fixture("arxiv-id.xml"))
            if ids == "nope":
                return httpx.Response(200, text=fixture("arxiv-bad-id.xml"))
            if ids:
                return httpx.Response(200, text=fixture("arxiv-missing.xml"))
            return httpx.Response(200, text=fixture("arxiv-search.xml"))
        return httpx.Response(500, text=f"unexpected {url}")

    return httpx.Client(transport=httpx.MockTransport(handle), headers={"User-Agent": scholar.USER_AGENT})


@pytest.fixture
def http():
    return registries()


# --- ids ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, doi", [
    ("https://doi.org/10.1038/Nature14539", "10.1038/nature14539"), ("doi:10.1038/nature14539", "10.1038/nature14539"),
    ("10.1038/nature14539", "10.1038/nature14539"), ("https://arxiv.org/abs/1706.03762", None), ("", None), (None, None),
])
def test_dois_in_their_spellings(text, doi):
    assert scholar.normalize_doi(text) == doi


@pytest.mark.parametrize("text, ident", [
    ("arXiv:1706.03762v5", "1706.03762"), ("https://arxiv.org/abs/1706.03762", "1706.03762"), ("1706.03762", "1706.03762"),
    ("https://arxiv.org/pdf/1810.04805v2.pdf", "1810.04805"), ("hep-th/9901001v2", "hep-th/9901001"), ("not an id", None),
])
def test_arxiv_ids_in_their_spellings(text, ident):
    assert scholar.normalize_arxiv(text) == ident


def test_openalex_ids_and_title_similarity():
    assert scholar.normalize_openalex("https://openalex.org/W2963403868") == "W2963403868"
    assert scholar.normalize_openalex("W123") == "W123" and scholar.normalize_openalex("nope") is None
    assert scholar.title_similarity("Attention is all you need", "Attention Is All You Need.") == 1.0
    assert scholar.title_similarity("Attention is all you need", "Deep learning") < 0.5


# --- each registry ---------------------------------------------------------------------------------

def test_openalex_results_carry_ids_authors_and_the_abstract_put_back_in_order(http):
    papers = scholar.search_openalex("attention", 5, http)
    assert [p.title for p in papers] == ["Attention Is All You Need", "Deep learning"]
    first = papers[0]
    assert (first.doi, first.arxiv, first.openalex) == ("10.48550/arxiv.1706.03762", "1706.03762", "W2963403868")
    assert first.authors[0] == "Ashish Vaswani" and first.year == 2017 and first.cited_by == 120000
    assert first.abstract == "The dominant sequence transduction models are based on complex recurrent networks."
    assert papers[1].abstract is None and papers[1].venue == "Nature" and papers[1].source == "openalex"


def test_openalex_lookup_by_doi_or_id_and_a_missing_one(http):
    assert scholar.openalex_work("https://doi.org/10.1038/nature14539", http).openalex == "W2250539671"
    assert scholar.openalex_work("W2250539671", http).title == "Deep learning"
    assert scholar.openalex_work("10.1000/nope", http) is None and scholar.openalex_work("garbage", http) is None


def test_crossref_results_and_lookup(http):
    papers = scholar.search_crossref("deep learning", 5, http)
    assert papers[0].doi == "10.1038/nature14539" and papers[0].authors == ["Yann LeCun", "Yoshua Bengio", "Geoffrey Hinton"]
    assert papers[0].year == 2015 and papers[0].venue == "Nature" and papers[0].cited_by == 65000
    assert papers[0].abstract.startswith("Deep learning allows") and "<jats" not in papers[0].abstract
    assert papers[1].year == 1998 and papers[1].source == "crossref"
    assert scholar.crossref_work("doi:10.1038/nature14539", http).title == "Deep learning"
    assert scholar.crossref_work("10.1000/nope", http) is None


def test_arxiv_results_lookup_missing_and_malformed_ids(http):
    papers = scholar.search_arxiv("attention", 5, http)
    assert [p.arxiv for p in papers] == ["1706.03762", "1810.04805"]
    assert papers[0].authors[0] == "Ashish Vaswani" and papers[0].year == 2017 and papers[0].url == "https://arxiv.org/abs/1706.03762"
    assert papers[1].title == "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding"
    assert scholar.arxiv_work("arXiv:1706.03762v7", http).title == "Attention Is All You Need"
    assert scholar.arxiv_work("9999.99999", http) is None
    assert scholar.arxiv_work("nope", http) is None  # arXiv's "Error" entry is not a paper


def test_a_registry_that_is_down_is_an_error_with_its_name(http):
    with pytest.raises(ScholarError, match="OpenAlex answered HTTP 503"):
        scholar.search_openalex("x", 5, registries(down=("openalex",)))
    with pytest.raises(ScholarError, match="arXiv couldn't be reached"):
        scholar.search_arxiv("x", 5, httpx.Client(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("no")))))


# --- all together ------------------------------------------------------------------------------------

def test_search_merges_the_same_paper_from_several_registries_and_keeps_every_id(http):
    papers, notes = scholar.search("attention is all you need", limit=5, http=http)
    assert notes == []
    titles = [p.title for p in papers]
    assert titles.count("Attention Is All You Need") == 1 and titles.count("Deep learning") == 1  # merged by DOI / arXiv id
    attention = next(p for p in papers if p.title == "Attention Is All You Need")
    assert attention.arxiv == "1706.03762" and attention.openalex == "W2963403868" and attention.source == "openalex"
    deep = next(p for p in papers if p.title == "Deep learning")
    assert deep.abstract.startswith("Deep learning allows")  # filled in from Crossref, which had it
    assert any(p.title.startswith("BERT") for p in papers) and any(p.year == 1998 for p in papers)


def test_one_registry_down_does_not_empty_the_result():
    papers, notes = scholar.search("attention", http=registries(down=("crossref",)))
    assert len(papers) >= 2 and notes == ["Crossref answered HTTP 503"]
    papers, notes = scholar.search("attention", sources=("arxiv", "nope"), http=registries())
    assert [p.source for p in papers] == ["arxiv", "arxiv"] and "unknown source 'nope'" in notes[0]
    with pytest.raises(ScholarError, match="no query"):
        scholar.search("   ", http=registries())


def test_the_text_for_the_agent_puts_ids_first(http):
    papers, notes = scholar.search("attention", http=registries(down=("crossref",)))
    text = scholar.describe(papers, notes)
    assert text.startswith("1. Attention Is All You Need (2017) — Ashish Vaswani, ")
    assert "et al." in text and "ids: doi: 10.48550/arxiv.1706.03762, arxiv: 1706.03762, openalex: W2963403868" in text
    assert "Notes: Crossref answered HTTP 503" in text
    assert scholar.describe([], ["x"]) == "No papers found.\n- x"


def test_requests_name_the_pack_and_never_the_user(http):
    log = []
    scholar.search("attention", http=registries(log=log))
    assert len(log) == 3 and all("mailto" not in url and "@" not in url for url in log)
    assert scholar.client().headers["User-Agent"].startswith("spare-gpu-pack/")
