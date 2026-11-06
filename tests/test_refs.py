"""verify_refs against recorded registry answers: a reference is kept only when its id resolves
and the title and authors match; a made-up DOI, a wrong title, other authors, a URL-only source
that doesn't carry the title, and a reference with nothing to check are all dropped, and the
verdicts are written back into sources.json."""

import json

import httpx
import pytest

from gpu_token_utilization import refs
from gpu_token_utilization.refs import Reference, Verdict

from test_scholar import registries


def http_with_pages(pages: dict):
    base = registries()

    def handle(request):
        url = str(request.url)
        for prefix, (status, body) in pages.items():
            if url.startswith(prefix):
                return httpx.Response(status, text=body)
        return base._transport.handle_request(request)

    return httpx.Client(transport=httpx.MockTransport(handle))


DEEP = {"key": "lecun2015", "title": "Deep learning", "authors": ["Yann LeCun", "Yoshua Bengio", "Geoffrey Hinton"],
        "year": 2015, "doi": "https://doi.org/10.1038/nature14539"}
ATTENTION = {"key": "vaswani2017", "title": "Attention is all you need", "authors": "Vaswani, A. and Shazeer, N.",
             "year": 2017, "arxiv": "arXiv:1706.03762"}


def test_references_are_read_from_sources_json_in_their_spellings():
    ref = Reference.from_dict(ATTENTION, 1)
    assert ref.arxiv == "1706.03762" and ref.authors == ["Vaswani, A.", "Shazeer, N."] and ref.year == 2017
    assert Reference.from_dict({"url": "https://arxiv.org/abs/1810.04805"}, 2).arxiv == "1810.04805"
    assert Reference.from_dict({"url": "https://doi.org/10.1038/nature14539"}, 3).doi == "10.1038/nature14539"
    assert Reference.from_dict({}, 4).key == "ref4"


def test_authors_match_by_last_name_in_any_order_or_by_half():
    found = ["Yann LeCun", "Yoshua Bengio", "Geoffrey Hinton"]
    assert refs.authors_match(["Y. LeCun", "others"], found) is True
    assert refs.authors_match(["Bengio, Yoshua", "Hinton, G."], found) is True  # reordered: half match
    assert refs.authors_match(["Nobody", "Hinton"], found) is True  # half of them
    assert refs.authors_match(["Nobody", "Noone", "Hinton"], found) is False
    assert refs.authors_match(None, found) is None and refs.authors_match(["X"], []) is None


def test_a_doi_that_resolves_with_matching_title_and_authors_is_kept():
    verdict = refs.verify(Reference.from_dict(DEEP, 1), registries())
    assert verdict.ok and verdict.reason == "resolves and matches" and verdict.found.doi == "10.1038/nature14539"
    assert verdict.title_match == 1.0


def test_an_arxiv_id_that_resolves_is_kept_even_with_initials_for_authors():
    verdict = refs.verify(Reference.from_dict(ATTENTION, 1), registries())
    assert verdict.ok and verdict.found.arxiv == "1706.03762"


def test_a_made_up_doi_is_dropped():
    verdict = refs.verify(Reference.from_dict({**DEEP, "doi": "10.1000/spare-gpu-no-such-doi"}, 1), registries())
    assert not verdict.ok and verdict.reason == "no registry has DOI 10.1000/spare-gpu-no-such-doi"


def test_a_real_id_with_the_wrong_title_or_authors_or_year_is_dropped():
    wrong_title = refs.verify(Reference.from_dict({**DEEP, "title": "A survey of quantum particle swarms"}, 1), registries())
    assert not wrong_title.ok and "different title: 'Deep learning'" in wrong_title.reason and wrong_title.title_match < 0.5
    wrong_authors = refs.verify(Reference.from_dict({**DEEP, "authors": ["Alan Turing", "Ada Lovelace"]}, 1), registries())
    assert not wrong_authors.ok and wrong_authors.reason.startswith("the id resolves to other authors: Yann LeCun")
    wrong_year = refs.verify(Reference.from_dict({**DEEP, "year": 2009}, 1), registries())
    assert not wrong_year.ok and wrong_year.reason == "the year is 2015, not 2009"
    assert refs.verify(Reference.from_dict({**DEEP, "year": 2016}, 1), registries()).ok  # a year off is tolerated


def test_a_url_only_source_must_answer_and_carry_the_title():
    http = http_with_pages({
        "https://example.org/good": (200, "<html><title>Deep learning in Nature</title><p>Deep learning allows...</p></html>"),
        "https://example.org/other": (200, "<html><body>Cat pictures</body></html>"),
        "https://example.org/gone": (404, "nope"),
    })
    assert refs.verify(Reference.from_dict({"key": "a", "title": "Deep learning", "url": "https://example.org/good"}, 1), http).ok
    other = refs.verify(Reference.from_dict({"key": "b", "title": "Deep learning allows models", "url": "https://example.org/other"}, 1), http)
    assert not other.ok and "doesn't carry the title" in other.reason
    gone = refs.verify(Reference.from_dict({"key": "c", "title": "x", "url": "https://example.org/gone"}, 1), http)
    assert not gone.ok and gone.reason == "the URL answered HTTP 404"
    assert refs.verify(Reference.from_dict({"key": "d", "url": "https://example.org/good"}, 1), http).reason == "the URL answers (no title to check)"


def test_nothing_to_check_and_a_registry_outage_are_both_dropped_with_the_reason():
    nothing = refs.verify(Reference.from_dict({"key": "x", "title": "Some paper"}, 1), registries())
    assert not nothing.ok and nothing.reason == "no DOI, arXiv id, OpenAlex id or URL to check"
    outage = refs.verify(Reference.from_dict({**DEEP, "doi": "10.1000/nope"}, 1), registries(down=("openalex",)))
    assert not outage.ok and outage.reason.startswith("couldn't check it: OpenAlex")


def test_verify_file_writes_the_verdicts_back_and_describes_what_to_drop(tmp_path):
    folder = tmp_path / "research" / "transformers"
    folder.mkdir(parents=True)
    (folder / "sources.json").write_text(json.dumps({"sources": [
        DEEP, ATTENTION, {"key": "fake2021", "title": "Quantum swarms in IoV", "authors": ["A. Nobody"], "doi": "10.1000/fake"},
    ]}), encoding="utf-8")
    verdicts, path = refs.verify_file(folder, http=registries())
    assert [v.ok for v in verdicts] == [True, True, False] and path == folder / "sources.json"
    saved = json.loads(path.read_text(encoding="utf-8"))["sources"]
    assert saved[0]["verified"] is True and saved[0]["resolved"]["doi"] == "10.1038/nature14539" and saved[0]["title"] == "Deep learning"
    assert saved[2]["verified"] is False and saved[2]["verify_reason"] == "no registry has DOI 10.1000/fake"
    text = refs.describe(verdicts, path)
    assert text.startswith(f"Checked 3 reference(s) in {path}: 2 verified, 1 to drop.")
    assert "- DROP fake2021: no registry has DOI 10.1000/fake" in text and "- keep lecun2015: resolves and matches (https://doi.org/10.1038/nature14539)" in text
    assert "Never keep a reference the registries don't have" in text


def test_a_bare_list_is_accepted_and_junk_is_refused(tmp_path):
    good = tmp_path / "sources.json"
    good.write_text(json.dumps([DEEP]), encoding="utf-8")
    assert refs.verify_file(good, http=registries())[0][0].ok
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"sources": "nope"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must hold a list of references"):
        refs.verify_file(bad, http=registries())


def test_verdict_dicts_are_json_friendly():
    v = Verdict("k", True, "r")
    assert refs.to_dicts([v])[0]["ok"] is True and json.dumps(refs.to_dicts([v]))
