"""`<jargon>` markers must never reach a PDF.

The cross-domain pedagogy brief asks the writer to wrap technical keywords in
`<jargon term="...">` so the interactive player can show a definition on
demand (client UAT 5.2). A PDF has no such affordance, and Jinja autoescapes
the tag into visible text - which is exactly what shipped into a generated
course before this fix.

The contract these tests pin down:
* the tag is stripped at the render boundary, keeping the inner term;
* the stored Course Document keeps its tags, so the player still works.
"""

from __future__ import annotations

import pytest

from app.course.templates.registry import load_template
from app.render.html_renderer import render_document_html, strip_jargon
from app.schemas.blocks import BlockType
from app.schemas.document import Block, BlockMeta, CourseDocument, DocumentMeta, Page, PageSize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            'The <jargon term="Baseline audit">baseline audit</jargon> reveals patterns.',
            "The baseline audit reveals patterns.",
        ),
        ('<jargon term="Pareto Principle">Pareto Principle</jargon>', "Pareto Principle"),
        # Headings are uppercased by CSS, which made the leak especially loud.
        ('RUN ONE <JARGON TERM="PTPR">PTPR</JARGON> CYCLE', "RUN ONE PTPR CYCLE"),
        # A term may legitimately contain ">", which a naive [^>]* would trip on.
        ('<jargon term="Focus > Busyness">Focus > Busyness</jargon> wins', "Focus > Busyness wins"),
        ("<jargon term='single > quoted'>x</jargon>", "x"),
        # Malformed markup must not leak either.
        ('unclosed <jargon term="x">tail', "unclosed tail"),
        ("<jargon>bare</jargon>", "bare"),
        ("</jargon> orphan", " orphan"),
        # Unrelated text is untouched.
        ("plain > angle bracket stays", "plain > angle bracket stays"),
        ("no tags at all", "no tags at all"),
    ],
)
def test_strip_jargon_keeps_the_term_and_drops_the_tag(raw: str, expected: str) -> None:
    assert strip_jargon(raw) == expected


def test_strip_jargon_walks_nested_content() -> None:
    """Quiz options, table rows and list items are nested structures, not
    flat strings - the whole block content is walked."""
    content = {
        "title": '<jargon term="T">Term</jargon> title',
        "items": ['a <jargon term="X">x</jargon> b'],
        "rows": [{"cells": ['<jargon term="Q">q</jargon>', "plain"]}],
        "questions": [{"options": ['<jargon term="O">o</jargon>']}],
        "level": 2,
        "flag": True,
        "nothing": None,
    }
    assert strip_jargon(content) == {
        "title": "Term title",
        "items": ["a x b"],
        "rows": [{"cells": ["q", "plain"]}],
        "questions": [{"options": ["o"]}],
        "level": 2,
        "flag": True,
        "nothing": None,
    }


def test_strip_jargon_does_not_mutate_its_input() -> None:
    """The stored document must keep its tags for the interactive player."""
    content = {"text": '<jargon term="T">Term</jargon>', "items": ['<jargon term="I">i</jargon>']}
    original = {"text": '<jargon term="T">Term</jargon>', "items": ['<jargon term="I">i</jargon>']}
    strip_jargon(content)
    assert content == original


def _document(blocks: list[Block]) -> CourseDocument:
    return CourseDocument(
        document_id="doc_jargon",
        course_id="crs_jargon",
        course_title="Time Management",
        template_id="non_technical_v1",
        meta=DocumentMeta(audience="Professionals"),
        pages=[Page(id="page_1", page_number=1, kind="content", size=PageSize(), blocks=blocks)],
    )


def test_rendered_html_carries_no_jargon_markup() -> None:
    """Every block type that can carry text, in one render."""
    blocks = [
        Block(
            id="block_head01",
            type=BlockType.HEADING,
            content={"text": 'The <jargon term="PTPR">PTPR</jargon> Loop', "level": 2},
            meta=BlockMeta(),
        ),
        Block(
            id="block_para01",
            type=BlockType.PARAGRAPH,
            content={"text": 'Run a <jargon term="Baseline audit">baseline audit</jargon> first.'},
            meta=BlockMeta(),
        ),
        Block(
            id="block_obj001",
            type=BlockType.LEARNING_OBJECTIVES,
            content={"items": ['Identify one <jargon term="Metric">on-the-job metric</jargon>.']},
            meta=BlockMeta(),
        ),
        Block(
            id="block_tbl001",
            type=BlockType.TABLE,
            content={
                "columns": ["Step", "Proof"],
                "rows": [{"cells": ['<jargon term="Protect">Protect</jargon>', "Calendar hold"]}],
                "caption": 'Weekly <jargon term="PTPR">PTPR</jargon> ritual.',
            },
            meta=BlockMeta(),
        ),
        Block(
            id="block_quiz01",
            type=BlockType.QUIZ,
            content={
                "questions": [
                    {
                        "question": 'Apply the <jargon term="Pareto">Pareto Principle</jargon>?',
                        "options": ['Pick the <jargon term="Vital few">vital few</jargon>.'],
                        "answer": 'The <jargon term="Vital few">vital few</jargon>.',
                    }
                ]
            },
            meta=BlockMeta(),
        ),
        Block(
            id="block_img001",
            type=BlockType.IMAGE,
            content={
                "path": "assets/image_001.svg",
                "caption": 'Three principles feed the <jargon term="PTPR">PTPR</jargon> loop.',
            },
            meta=BlockMeta(),
        ),
    ]
    html = render_document_html(_document(blocks), load_template("non_technical_v1"))

    # Neither the raw tag nor its escaped form may appear.
    assert "jargon" not in html.lower()
    assert "&lt;jargon" not in html

    # The terms themselves survive.
    for term in ("PTPR", "baseline audit", "on-the-job metric", "Protect", "vital few"):
        assert term in html


def test_rendering_leaves_the_source_document_untouched() -> None:
    """Rendering is a projection: the player still needs these tags."""
    tagged = 'Run a <jargon term="Baseline audit">baseline audit</jargon>.'
    block = Block(
        id="block_para02",
        type=BlockType.PARAGRAPH,
        content={"text": tagged},
        meta=BlockMeta(),
    )
    document = _document([block])
    render_document_html(document, load_template("non_technical_v1"))
    assert document.pages[0].blocks[0].content["text"] == tagged


def test_inline_fragments_are_stripped() -> None:
    """Concept/TOC fragments are generated HTML built from the same AI text,
    and are inlined into the page rather than escaped - so they need the same
    treatment or the tag renders as live markup."""
    block = Block(
        id="block_conc01",
        type=BlockType.IMAGE,
        content={"path": "assets/image_002.html", "kind": "concept_experience"},
        meta=BlockMeta(),
    )
    html = render_document_html(
        _document([block]),
        load_template("non_technical_v1"),
        inline_fragments={
            "assets/image_002.html": '<div>A <jargon term="T">term</jargon> here</div>'
        },
    )
    assert "jargon" not in html.lower()
    assert "A term here" in html
