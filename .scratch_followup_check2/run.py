import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.course.templates.registry import load_template
from app.schemas.blocks import BlockType
from app.schemas.document import Block, CourseDocument, Page
from app.services.image_service import ImageService, chapter_section_context
from app.services.openai_service import get_ai_client
from app.core.config import get_settings

OUT_DIR = Path(__file__).resolve().parent

CASES = [
    {
        "key": "closures",
        "course_title": "Advanced Python",
        "chapter_title": "Closures and Lexical Scope",
        "section_title": "How a Closure Remembers",
        "objective": "Explain how a closure keeps a reference to a variable from its enclosing scope after that scope has finished executing",
        "image_prompt": (
            "A sealed glass jar holding one glowing marble, sitting alone on an empty shelf "
            "after everything else around it has been cleared away, clean modern digital "
            "illustration, no text, no letters, no labels"
        ),
    },
    {
        "key": "compound_interest",
        "course_title": "Personal Finance Fundamentals",
        "chapter_title": "Compound Interest",
        "section_title": "How Growth Builds on Growth",
        "objective": "Explain how compound interest causes a balance to grow faster over time because each period's interest is calculated on the previous period's total, not just the original amount",
        "image_prompt": (
            "A single tree ring at the center of a cut log, surrounded by several more rings "
            "each wider than the last, growing outward, clean modern digital illustration, no "
            "text, no letters, no labels"
        ),
    },
]


def build_document(case: dict) -> tuple[CourseDocument, str]:
    blocks = [
        Block(type=BlockType.HEADING, content={"text": case["chapter_title"], "level": 1}),
        Block(type=BlockType.HEADING, content={"text": case["section_title"], "level": 2}),
        (img := Block(
            type=BlockType.IMAGE,
            content={
                "kind": "illustration",
                "illustration_style": "section_intro",
                "prompt": case["image_prompt"],
            },
        )),
        Block(type=BlockType.LEARNING_OBJECTIVES, content={"items": [case["objective"]]}),
    ]
    document = CourseDocument(
        document_id="d", course_id=case["key"], course_title=case["course_title"],
        template_id="technical", pages=[Page(blocks=blocks)],
    )
    return document, img.id


async def main():
    settings = get_settings()
    ai = get_ai_client()
    template = load_template("technical")
    print(f"is_mock={ai.is_mock} provider={settings.image_provider}")
    if ai.is_mock:
        print("ABORT: mock client")
        return

    for case in CASES:
        document, img_id = build_document(case)
        context_map = chapter_section_context(document)
        chapter_title, section_title, key_concept = context_map[img_id]
        block = next(b for _, b in document.iter_blocks() if b.id == img_id)
        final_prompt = ImageService.build_prompt(
            block, template, document.course_title,
            chapter_title=chapter_title, section_title=section_title, key_concept=key_concept,
        )
        started = time.perf_counter()
        data = await ai.image(prompt=final_prompt, size=settings.image_size)
        latency = time.perf_counter() - started
        out = OUT_DIR / f"{case['key']}.png"
        out.write_bytes(data)
        print(f"{case['key']}: {latency:.1f}s -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
