"""Table of contents detection, page calibration, chunking strategies and hybrid retrieval."""
import asyncio
import json

import pytest

from shared.llm import LLMResult
from worker.extract.chunking import choose_strategy, chunk_document, split_text
from worker.extract.document import Document, OutlineItem, Page, load_text
from worker.extract.retrieval import BM25, EmbeddingError, HybridIndex
from worker.extract.toc import TocResult, detect_toc, find_toc_pages_by_rules, outline_from_headings, score_toc_page

FILLER = "The parties shall comply with the policies in the provider manual during the term. " * 20


def contract(offset_pages: int = 2, dotted: str = " . . . . . . ") -> Document:
    """Cover, one contents page, then body pages whose printed number = physical - offset_pages."""
    titles = ["ARTICLE 1. Definitions", "ARTICLE 2. Payment Terms", "ARTICLE 3. Insurance", "ARTICLE 4. Termination",
              "ARTICLE 5. Governing Law"]
    printed = [1, 3, 5, 7, 9]
    pages = [Page(1, "PROVIDER AGREEMENT\nbetween A and B", "text_layer"),
             Page(2, "TABLE OF CONTENTS\n" + "\n".join(f"{t}{dotted}{p}" for t, p in zip(titles, printed)), "text_layer")]
    body = {p + offset_pages: t for t, p in zip(titles, printed)}
    for n in range(3, 12 + offset_pages):
        text = (f"{body[n].upper()}\n" if n in body else "") + FILLER + f"\nPage {n - offset_pages}"
        if n == 9 + offset_pages - 2:
            text += "\nEither party may terminate on 120 days notice."
        pages.append(Page(n, text, "text_layer"))
    return Document("c.pdf", "pdf", pages)


class StubTocLLM:
    def __init__(self, scan_pages=None, entries=None, fail=False):
        self.scan_pages, self.entries, self.fail, self.calls = scan_pages, entries, fail, []

    async def chat(self, system, user, *, json_mode=False, max_tokens=4096, json_schema=None):
        self.calls.append(system[:30])
        if self.fail:
            raise RuntimeError("engine down")
        if "decide which" in system:
            return LLMResult(text=json.dumps({"toc_pages": self.scan_pages or []}))
        return LLMResult(text=json.dumps({"document_type": "provider contract", "summary": "A contract.",
                                          "entries": self.entries or []}))


def test_contents_page_found_by_rules_and_calibrated_to_physical_pages():
    doc = contract(offset_pages=2)
    assert find_toc_pages_by_rules(doc, 10) == [2]
    toc = asyncio.run(detect_toc(doc, None))
    assert toc.source == "toc_page_rules" and toc.page_offset == 2
    assert [(e.title, e.page, e.printed_page) for e in toc.entries][:2] == [("ARTICLE 1. Definitions", 3, "1"),
                                                                             ("ARTICLE 2. Payment Terms", 5, "3")]
    assert toc.entries[0].end_page == 5 and toc.entries[-1].end_page == doc.page_count
    assert toc.section("s4").title == "ARTICLE 4. Termination"


@pytest.mark.parametrize("leader", [" ........ ", " . . . . ", "……", "   "])
def test_dot_leader_variants(leader):
    ratio, count, heading = score_toc_page(contract(dotted=leader).page(2).text)
    assert heading and count == 5


def test_prose_with_trailing_numbers_is_not_a_contents_page():
    text = "\n".join(f"The fee was raised in year {y}" for y in (2019, 2015, 2021, 2012, 2020, 2011, 2018))
    assert score_toc_page(text)[0] == 0.0


def test_llm_structures_contents_pages_and_scans_when_rules_fail():
    doc = contract(offset_pages=2)
    llm = StubTocLLM(entries=[{"title": "ARTICLE 3. Insurance", "level": 1, "page": "5"},
                              {"title": "ARTICLE 4. Termination", "level": 1, "page": "7"},
                              {"title": "ARTICLE 5. Governing Law", "level": 1, "page": "9"}])
    toc = asyncio.run(detect_toc(doc, llm))
    assert toc.document_type == "provider contract" and [e.page for e in toc.entries] == [7, 9, 11]
    # Without a contents heading or leaders the rules miss it; the LLM scan points at page 2.
    doc.pages[1] = Page(2, "Overview\n" + "\n".join(["ARTICLE 3. Insurance 5", "ARTICLE 4. Termination 7"]), "text_layer")
    llm2 = StubTocLLM(scan_pages=[2], entries=llm.entries)
    toc2 = asyncio.run(detect_toc(doc, llm2))
    assert toc2.source == "toc_page_llm" and toc2.toc_pages == [2] and len(llm2.calls) == 2


def test_llm_failure_falls_back_to_the_rule_parser():
    toc = asyncio.run(detect_toc(contract(), StubTocLLM(fail=True)))
    assert toc.found and len(toc.entries) == 5 and any("failed" in n for n in toc.notes)


def test_bookmarks_win_and_headings_are_the_last_resort():
    doc = contract()
    doc.outline = [OutlineItem("Intro", 1, 1), OutlineItem("Terms", 1, 4), OutlineItem("Fees", 2, 6)]
    toc = asyncio.run(detect_toc(doc, None))
    assert toc.source == "bookmarks" and [e.page for e in toc.entries] == [1, 4, 6]
    plain = load_text("# Intro\ntext\n## Scope\nmore text\nSECTION 3 Fees\nfee text\nSCHEDULE A Rates\n", "notes.md")
    assert [e.title for e in outline_from_headings(plain)] == ["Intro", "Scope", "SECTION 3 Fees", "SCHEDULE A Rates"]
    none = asyncio.run(detect_toc(load_text("just a few words", "a.txt"), None))
    assert not none.found and none.source == "none"


def test_chunking_strategies():
    doc = contract()
    toc = asyncio.run(detect_toc(doc, None))
    strategy, _ = choose_strategy(doc, toc)
    assert strategy == "section"
    chunks = chunk_document(doc, toc, strategy, 200, 20)
    assert chunks[0].id == "c0001" and all(c.page_start <= c.page_end for c in chunks)
    term = [c for c in chunks if c.section_id == "s4"]
    assert term and all(c.section_title == "ARTICLE 4. Termination" for c in term)
    assert any("120 days notice" in c.text for c in term)

    csv = load_text("id,test,value\n" + "\n".join(f"P{i},LDL,{100 + i}" for i in range(400)), "labs.csv")
    assert choose_strategy(csv, TocResult(False, "none"))[0] == "tabular"
    rows = chunk_document(csv, TocResult(False, "none"), "tabular", 300, 0)
    assert len(rows) > 1 and all(c.text.startswith("id,test,value\n") for c in rows)

    form = load_text("Patient Name: A B\nDate of Birth: 2000-01-01\nMember ID: X1\nAllergies: none\n", "form.txt")
    assert choose_strategy(form, TocResult(False, "none"))[0] == "page"
    prose = load_text(FILLER * 5, "p.txt")
    assert choose_strategy(prose, TocResult(False, "none"))[0] == "recursive"


def test_split_text_cuts_at_boundaries_with_overlap():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 50 for i in range(10))
    spans = split_text(text, 600, 60)
    assert len(spans) > 3 and spans[-1][1] == len(text)
    for (a1, b1), (a2, _) in zip(spans, spans[1:]):
        assert a2 < b1  # overlap
    assert all(text[b - 2:b] in ("\n\n", ". ") or text[b - 1] == " " or b == len(text) for _, b in spans)


class FakeEmbedder:
    label = "fake:test"
    tokens_used = 0

    def __init__(self, fail=False):
        self.fail = fail

    async def embed(self, texts, *, query=False):
        if self.fail:
            raise EmbeddingError("down")
        vocab = ["terminate", "notice", "insurance", "payment", "law"]
        return [[float(t.lower().count(w)) + 0.01 for w in vocab] for t in texts]


def test_bm25_and_hybrid_search_with_filters():
    docs = ["termination notice of 120 days", "insurance limits per occurrence", "payment within 30 days"]
    scores = BM25(docs).scores("termination notice")
    assert scores.index(max(scores)) == 0
    doc = contract()
    toc = asyncio.run(detect_toc(doc, None))
    chunks = chunk_document(doc, toc, "section", 200, 20)
    index = HybridIndex(chunks, FakeEmbedder())
    asyncio.run(index.build())
    assert index.mode == "hybrid"
    hits = asyncio.run(index.search("terminate on 120 days notice", top_k=3))
    assert hits and "120 days notice" in hits[0].chunk.text
    assert asyncio.run(index.search("notice", top_k=3, section_id="s1")) == [] or all(
        h.chunk.section_id == "s1" for h in asyncio.run(index.search("notice", top_k=3, section_id="s1")))
    assert all(h.chunk.page_end >= 9 for h in asyncio.run(index.search("policies", top_k=5, page_from=9)))


def test_embedding_failure_degrades_to_keyword_search():
    chunks = chunk_document(contract(), TocResult(False, "none"), "recursive", 200, 0)
    index = HybridIndex(chunks, FakeEmbedder(fail=True))
    asyncio.run(index.build())
    assert index.mode == "keyword" and "keyword ranking only" in index.note
    assert asyncio.run(index.search("120 days notice", top_k=2))
