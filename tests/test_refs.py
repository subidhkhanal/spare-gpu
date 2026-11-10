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
    # other authors and no title to show it's the same paper: dropped
    wrong_authors = refs.verify(Reference.from_dict({**DEEP, "title": "", "authors": ["Alan Turing", "Ada Lovelace"]}, 1),
                                registries())
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


def test_nothing_to_check_is_dropped_but_a_registry_outage_leaves_it_unchecked():
    nothing = refs.verify(Reference.from_dict({"key": "x", "title": "Some paper"}, 1), registries())
    assert not nothing.ok and nothing.status == "drop" and nothing.reason == "no DOI, arXiv id, OpenAlex id or URL to check"
    outage = refs.verify(Reference.from_dict({**DEEP, "doi": "10.1000/nope"}, 1), registries(down=("openalex",)))
    assert outage.status == "unchecked" and outage.reason.startswith("couldn't check it: OpenAlex")
    assert outage.to_dict()["verified"] is None  # neither kept nor dropped


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


def test_the_report_is_built_from_its_sections_in_number_order_with_references_from_sources(tmp_path):
    sections = tmp_path / "sections"
    sections.mkdir()
    (sections / "10-open-questions.md").write_text("## Open questions\n\nStill open [b2020; a2019].\n", encoding="utf-8")
    (sections / "02-background.md").write_text("## Background\n\nIt began [a2019]. See [the docs](https://x.org) "
                                               "and [ghost].\n", encoding="utf-8")
    (tmp_path / "sources.json").write_text(json.dumps({"sources": [
        {"key": "b2020", "title": "Second", "authors": ["B Bee"], "year": 2020, "doi": "10.1/b"},
        {"key": "a2019", "title": "First", "authors": ["A Ay", "C Cee"], "year": 2019, "url": "https://a.org"},
    ]}), encoding="utf-8")
    report, summary = refs.build_report(tmp_path, "PSO")
    text = report.read_text(encoding="utf-8")
    assert text.index("## Background") < text.index("## Open questions")  # 02 before 10, not by string order
    assert text.endswith("## References\n\n- [a2019] A Ay, C Cee (2019). *First*. URL https://a.org\n"
                         "- [b2020] B Bee (2020). *Second*. DOI 10.1/b\n")
    assert "Cited but not in sources.json: ghost" in summary and "the docs" not in summary  # a link isn't a citation
    data = json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))
    data["sources"][1]["verified"] = False  # verify_refs dropped a2019
    (tmp_path / "sources.json").write_text(json.dumps(data), encoding="utf-8")
    report, summary = refs.build_report(tmp_path, "PSO")
    assert "[a2019] A Ay" not in report.read_text(encoding="utf-8") and "left out of References: a2019" in summary
    with pytest.raises(FileNotFoundError):
        refs.build_report(tmp_path / "empty")


def test_a_section_written_with_a_top_level_heading_is_moved_under_the_title(tmp_path):
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "01-abstract.md").write_text("# Abstract\n\nText.\n\n## Detail\n\n```\n# a comment\n```\n",
                                                          encoding="utf-8")
    (tmp_path / "sections" / "02-background.md").write_text("## Background\n\nMore.\n", encoding="utf-8")
    text = refs.build_report(tmp_path, "PSO")[0].read_text(encoding="utf-8")
    assert [line for line in text.splitlines() if line.startswith("#")] == [
        "# PSO", "## Abstract", "### Detail", "# a comment", "## Background", "## References"]  # code left alone


# --- collect_sources: the scouts' lists merged into sources.json ----------------------------------

def notes_file(folder, name, lines, body=""):
    path = folder / "notes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "```sources\n" + "\n".join(json.dumps(line) for line in lines) + "\n```\n", encoding="utf-8")
    return path


def test_the_same_source_from_two_scouts_becomes_one_entry_and_both_keys_still_cite_it(tmp_path):
    notes_file(tmp_path, "1-a.md", [
        {"key": "lecun2015", "title": "Deep learning", "doi": "https://doi.org/10.1038/NATURE14539", "quality": "A"},
        {"key": "blog", "title": "A blog", "url": "https://www.example.com/post/?utm_source=x#top", "quality": "C"}])
    notes_file(tmp_path, "2-b.md", [
        {"key": "deeplearning", "doi": "10.1038/nature14539", "year": 2015},  # same paper, another key
        {"key": "blog2", "url": "http://example.com/post", "date": "2026-01-02"},  # same page
        {"key": "nothing", "title": "No id at all"}])
    path, summary = refs.collect_sources(tmp_path)
    items = refs.load_sources(path)
    assert [i["key"] for i in items] == ["lecun2015", "blog"]
    assert items[0]["aliases"] == ["deeplearning"] and items[0]["year"] == 2015  # filled from the second scout
    assert items[1]["aliases"] == ["blog2"] and items[1]["date"] == "2026-01-02"
    assert "2 source(s) from 5 entr(ies) in 2 notes file(s); 2 duplicate(s) merged." in summary
    assert "Quality: 1 A (primary or authoritative), 0 B (reputable secondary), 1 C (weak" in summary
    assert "Left out, no DOI, arXiv id, OpenAlex id or URL: nothing (2-b.md)." in summary
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "01-a.md").write_text("## A\n\nNets learn [deeplearning; blog2].\n", encoding="utf-8")
    report, built = refs.build_report(tmp_path, "T")
    text = report.read_text(encoding="utf-8")
    assert "Nets learn [lecun2015; blog]." in text and "- [lecun2015]" in text and "not in sources.json" not in built


def test_one_key_for_two_different_sources_gets_the_later_renamed_in_its_notes_too(tmp_path):
    notes_file(tmp_path, "1-a.md", [{"key": "smith2024", "doi": "10.1000/aaa"}])
    later = notes_file(tmp_path, "2-b.md", [{"key": "smith2024", "doi": "10.1000/bbb"}],
                       body="## Findings\n- A quote [smith2024] and [x, smith2024].\n\n")
    path, summary = refs.collect_sources(tmp_path)
    assert [i["key"] for i in refs.load_sources(path)] == ["smith2024", "smith2024-2"]
    assert "smith2024 -> smith2024-2 in 2-b.md" in summary
    text = later.read_text(encoding="utf-8")
    assert "A quote [smith2024-2] and [x, smith2024-2]." in text and '"key": "smith2024-2"' in text


def test_urls_are_compared_without_their_noise():
    assert refs.normalize_url("https://WWW.Example.com/a/b/?utm_campaign=z&id=3#frag") == "example.com/a/b?id=3"
    assert refs.normalize_url("ftp://example.com/x") is None and refs.normalize_url(None) is None
    assert refs.identity({"url": "https://arxiv.org/abs/1706.03762v5"}) == refs.identity({"arxiv": "1706.03762"})


def test_collecting_needs_notes_and_skips_lines_that_arent_sources(tmp_path):
    with pytest.raises(FileNotFoundError):
        refs.collect_sources(tmp_path)
    path = tmp_path / "notes" / "1.md"
    path.parent.mkdir()
    path.write_text('```sources\nnot json\n{"title": "no key"}\n{"key": "ok", "url": "https://a.org"}\n```\n', encoding="utf-8")
    _, summary = refs.collect_sources(tmp_path)
    assert "1 source(s)" in summary and "2 line(s) in the sources lists couldn't be read" in summary and "1.md (2)" in summary


def test_a_preprint_cited_by_its_arxiv_doi_and_by_its_arxiv_id_is_one_source(tmp_path):
    # from a real run: Self-RAG came back from two scouts, once with arXiv's DOI and once with the bare id
    notes_file(tmp_path, "1-a.md", [{"key": "asai2023selfrag", "title": "Self-RAG", "arxiv": "2310.11511",
                                     "doi": "10.48550/arxiv.2310.11511", "url": "https://arxiv.org/abs/2310.11511"}])
    notes_file(tmp_path, "2-b.md", [{"key": "selfrag-2023", "title": "Self-RAG", "arxiv": "2310.11511v1"},
                                    {"key": "selfrag-doi", "doi": "https://doi.org/10.48550/arXiv.2310.11511"}])
    path, summary = refs.collect_sources(tmp_path)
    items = refs.load_sources(path)
    assert [i["key"] for i in items] == ["asai2023selfrag"]
    assert items[0]["aliases"] == ["selfrag-2023", "selfrag-doi"] and "2 duplicate(s) merged" in summary


def test_entries_sharing_any_one_id_are_one_source_and_the_kept_one_gains_the_other_ids(tmp_path):
    notes_file(tmp_path, "1-a.md", [{"key": "lewis2020", "doi": "10.5555/3495724.3496517"}])  # the NeurIPS DOI only
    notes_file(tmp_path, "2-b.md", [{"key": "rag-paper", "doi": "10.5555/3495724.3496517", "arxiv": "2005.11401"}])
    notes_file(tmp_path, "3-c.md", [{"key": "rag-arxiv", "url": "https://arxiv.org/abs/2005.11401v4"}])  # known only by arXiv
    path, summary = refs.collect_sources(tmp_path)
    items = refs.load_sources(path)
    assert [i["key"] for i in items] == ["lewis2020"] and items[0]["arxiv"] == "2005.11401"
    assert items[0]["aliases"] == ["rag-paper", "rag-arxiv"] and "1 source(s) from 3 entr(ies)" in summary
    assert refs.identities({"doi": "10.48550/arXiv.2005.11401"}) == ["arxiv:2005.11401"]


def test_two_papers_on_one_page_stay_two_sources_and_keep_their_own_dois(tmp_path):
    volume = "https://aclanthology.org/volumes/2023.acl-long/"
    notes_file(tmp_path, "1-a.md", [{"key": "p1", "doi": "10.18653/v1/2023.acl-long.1", "url": volume},
                                    {"key": "page", "url": "https://example.org/paper"}])
    notes_file(tmp_path, "2-b.md", [{"key": "p2", "doi": "10.18653/v1/2023.acl-long.7", "url": volume},
                                    {"key": "page-doi", "doi": "10.1000/xyz", "url": "https://example.org/paper"},
                                    {"key": "page-again", "url": "https://example.org/paper?utm_source=x"}])
    path, summary = refs.collect_sources(tmp_path)
    items = {i["key"]: i for i in refs.load_sources(path)}
    assert set(items) == {"p1", "p2", "page"}  # same page, different DOIs: two papers
    assert items["p1"]["doi"].endswith(".1") and items["p2"]["doi"].endswith(".7")
    # a source known only by its page takes the paper id of an entry for that page, and later page-only entries join it
    assert items["page"]["aliases"] == ["page-doi", "page-again"] and items["page"]["doi"] == "10.1000/xyz"


def test_an_entry_that_shows_two_kept_sources_are_one_paper_joins_them(tmp_path):
    notes_file(tmp_path, "1-a.md", [{"key": "byDoi", "doi": "10.1000/rag", "quality": "A"}])
    notes_file(tmp_path, "2-b.md", [{"key": "byArxiv", "arxiv": "2005.11401", "year": 2020}])
    notes_file(tmp_path, "3-c.md", [{"key": "both", "doi": "10.1000/rag", "arxiv": "2005.11401"}])
    path, summary = refs.collect_sources(tmp_path)
    items = refs.load_sources(path)
    assert [i["key"] for i in items] == ["byDoi"] and items[0]["aliases"] == ["byArxiv", "both"]
    assert items[0]["arxiv"] == "2005.11401" and items[0]["year"] == 2020 and items[0]["quality"] == "A"
    assert "1 source(s) from 3 entr(ies)" in summary and "2 duplicate(s) merged" in summary


def test_old_style_arxiv_ids_in_arxivs_doi_match_and_an_unreadable_one_stays_a_doi():
    assert refs.identities({"doi": "10.48550/arXiv.math.AG/0101001"}) == refs.identities({"arxiv": "math.AG/0101001"})
    assert refs.identities({"doi": "https://doi.org/10.48550/arXiv.2310.11511"}) == ["arxiv:2310.11511"]
    assert refs.identities({"doi": "10.48550/arxiv.nonsense"}) == ["doi:10.48550/arxiv.nonsense"]
    assert refs.identities({"url": "https://example.org/a"}) == ["url:example.org/a"]
    assert refs.identities({"doi": "10.1000/x", "url": "https://example.org/a"}) == ["doi:10.1000/x"]  # a page only when nothing better



# --- what a real run showed: "et al.", refusing sites, wrong author lists, the scouts' own formats --

def test_et_al_is_not_a_surname():
    assert [refs._last_name(n) for n in ("Es et al.", "Hao Xin et al.", "Magesh et al. 2024", "LeCun, Y.")] == [
        "es", "xin", "magesh", "lecun"]
    assert refs.authors_match(["LeCun et al."], ["Yann LeCun", "Yoshua Bengio"]) is True
    assert refs.verify(Reference.from_dict({**DEEP, "authors": ["LeCun et al."]}, 1), registries()).ok


def test_a_real_paper_with_the_wrong_authors_or_a_shortened_title_is_kept_and_corrected(tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    (folder / "sources.json").write_text(json.dumps({"sources": [
        {**DEEP, "key": "a", "authors": ["Alan Turing"]},  # the exact title: the paper is real, the authors are wrong
        {**DEEP, "key": "b", "title": "Deep learning nets"},  # shortened or padded title, right authors
        {**DEEP, "key": "c", "authors": "unlisted (arXiv)"},  # no author given is nothing to compare
    ]}), encoding="utf-8")
    verdicts, path = refs.verify_file(folder, http=registries())
    assert [v.status for v in verdicts] == ["keep", "keep", "keep"]
    saved = json.loads(path.read_text(encoding="utf-8"))["sources"]
    assert "authors were wrong and are corrected" in saved[0]["verify_reason"] and "Yann LeCun" in str(saved[0]["authors"])
    assert "Corrected in sources.json from the registries: a, b." in refs.describe(verdicts, path)
    assert saved[1]["title"] == "Deep learning" and "title was shortened" in saved[1]["verify_reason"]


def test_a_site_that_refuses_or_is_busy_leaves_the_source_unchecked_never_dropped(tmp_path):
    http = http_with_pages({"https://example.org/locked": (403, "no"), "https://example.org/busy": (429, "slow down")})
    for url in ("https://example.org/locked", "https://example.org/busy"):
        verdict = refs.verify(Reference.from_dict({"key": "k", "title": "A review", "url": url}, 1), http)
        assert verdict.status == "unchecked" and "says nothing about whether the page exists" in verdict.reason
    folder = tmp_path / "r"
    (folder / "sections").mkdir(parents=True)
    (folder / "sections" / "01-a.md").write_text("## A\n\nA claim [k].\n", encoding="utf-8")
    (folder / "sources.json").write_text(json.dumps({"sources": [{"key": "k", "title": "A review", "url": "https://example.org/locked"}]}),
                                         encoding="utf-8")
    verdicts, path = refs.verify_file(folder, http=http)
    text = refs.describe(verdicts, path)
    assert "0 to drop, 1 couldn't be checked." in text and "- UNCHECKED k:" in text and "are not DROP" in text
    report, built = refs.build_report(folder, "T")
    assert "(not checked automatically" in report.read_text(encoding="utf-8") and "DROP" not in built


def test_collect_sources_reads_the_formats_scouts_actually_wrote(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    # from a real run: pipe lines, JSON with a generic "id" and "rating", and a plain fence under a heading
    (notes / "1-a.md").write_text(
        "## Findings\n- x [lewis2020]\n\n```sources\n"
        "key | title | author/year | type | id | rating\n"
        "lewis2020 | Retrieval-Augmented Generation | Lewis, Perez, Piktus et al. / 2020 | paper | "
        "arXiv 2005.11401 (DOI 10.48550/arxiv.2005.11401) | A\n"
        "haleval2023 | HaluEval | Li, Cheng / 2023 | paper | https://aclanthology.org/2023.emnlp-main.397/ | A\n```\n",
        encoding="utf-8")
    (notes / "2-b.md").write_text(
        "```sources\n"
        '{"key": "rag", "title": "RAG", "author": "Lewis et al.", "year": "2020", "id": "arXiv:2005.11401", "rating": "A"}\n'
        '{"key": "ragas", "title": "RAGAS", "author/year": "Es et al. 2023", "id": "https://arxiv.org/abs/2309.15217", "rating": "B secondary"}\n'
        '{"key": "wrongfield", "title": "G", "doi": "arXiv:2404.07060", "quality": "a"}\n'
        "```\n", encoding="utf-8")
    (notes / "3-c.md").write_text("## Sources\n\n```json\n"
                                  '{"key": "realm", "title": "REALM", "url": "https://arxiv.org/abs/2002.08909", "quality": "A"}\n'
                                  "```\n", encoding="utf-8")
    (notes / "4-d.md").write_text("## Findings\nnothing listed\n", encoding="utf-8")
    path, summary = refs.collect_sources(tmp_path)
    items = {i["key"]: i for i in refs.load_sources(path)}
    assert set(items) == {"lewis2020", "haleval2023", "ragas", "wrongfield", "realm"}
    assert items["lewis2020"]["aliases"] == ["rag"] and items["lewis2020"]["arxiv"] == "2005.11401"
    assert items["lewis2020"]["authors"] == "Lewis, Perez, Piktus et al." and items["lewis2020"]["year"] == 2020
    assert items["haleval2023"]["doi"] == "10.18653/v1/2023.emnlp-main.397"  # read from the ACL Anthology link
    assert items["ragas"]["quality"] == "B" and items["ragas"]["arxiv"] == "2309.15217" and items["ragas"]["year"] == 2023
    assert items["wrongfield"]["arxiv"] == "2404.07060" and "doi" not in items["wrongfield"] and items["wrongfield"]["quality"] == "A"
    assert "No sources list found in: 4-d.md." in summary and "never edit sources.json by hand" in summary
    assert "couldn't be read" not in summary  # the header row of the pipe table isn't a bad line


def test_an_acl_anthology_link_is_checked_by_its_doi_and_an_unreadable_pdf_is_unchecked():
    ref = Reference.from_dict({"key": "r", "title": "RAGTruth", "url": "https://aclanthology.org/2024.acl-long.585.pdf"}, 1)
    assert ref.doi == "10.18653/v1/2024.acl-long.585"
    http = http_with_pages({"https://example.org/paper.pdf": (200, "%PDF-1.5 binary stream"),
                            "https://example.org/plain.pdf": (200, "%PDF-1.4 Deep learning allows computational models")})
    locked = refs.verify(Reference.from_dict({"key": "a", "title": "A study of things", "url": "https://example.org/paper.pdf"}, 1), http)
    assert locked.status == "unchecked" and "PDF" in locked.reason
    readable = refs.verify(Reference.from_dict({"key": "b", "title": "Deep learning allows models", "url": "https://example.org/plain.pdf"}, 1), http)
    assert readable.ok  # a PDF whose text carries the title is checked like any page


def test_a_preprint_and_its_published_doi_with_the_same_title_are_one_source_but_two_dois_are_two(tmp_path):
    notes_file(tmp_path, "1-a.md", [{"key": "clash-eval", "title": "ClashEval: Quantifying the tug-of-war between an LLM's internal prior",
                                     "doi": "10.52202/079017-1053"}])
    notes_file(tmp_path, "2-b.md", [{"key": "wu2024clash", "title": "ClashEval: quantifying the tug of war between an LLM's internal prior",
                                     "arxiv": "2404.10198"},
                                    {"key": "other", "title": "ClashEval: Quantifying the tug-of-war between an LLM's internal prior",
                                     "doi": "10.9999/a-different-work"},
                                    {"key": "short", "title": "Deep learning", "url": "https://example.org/x"},
                                    {"key": "short2", "title": "Deep learning", "url": "https://example.org/y"}])
    path, _ = refs.collect_sources(tmp_path)
    items = {i["key"]: i for i in refs.load_sources(path)}
    assert items["clash-eval"]["aliases"] == ["wu2024clash"] and items["clash-eval"]["arxiv"] == "2404.10198"
    assert "other" in items and "short2" in items  # another DOI is another work; a short title proves nothing
