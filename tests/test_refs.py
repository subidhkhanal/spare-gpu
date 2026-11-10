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
    assert "1 source(s)" in summary and "2 line(s) in ```sources fences" in summary
