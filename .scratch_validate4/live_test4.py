import sys, time, asyncio, logging
sys.path.insert(0, "backend")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

from app.core.config import get_settings
from app.course.templates.registry import load_template
from app.schemas.document import Block
from app.services.image_service import ImageService
from app.services.openai_service import get_ai_client

CASES = [
    ("1_newtons_second_law", (
        "Two solid blocks of clearly different sizes, one small and one much "
        "larger, side by side, each being pushed by an identical-length arrow "
        "from the left at the same height - the small block trailing a long "
        "motion-blur streak and the large block trailing a much shorter "
        "motion-blur streak, showing the same force producing more "
        "acceleration on less mass. Flat vector educational physics diagram, "
        "clean composition, no text, no numbers, no formula"
    )),
    ("2_supply_demand", (
        "Two smooth curves on a plain two-axis frame with no numbers or text - "
        "one curve sloping upward from lower-left to upper-right, a second "
        "curve sloping downward from upper-left to lower-right, the two curves "
        "crossing at exactly one point marked with a small solid dot, with "
        "light dashed guide-lines dropping straight down and straight across "
        "from that crossing point to the two axis lines. Clean minimal "
        "economics textbook diagram, no text, no numbers"
    )),
    ("3_quadratic_function", (
        "A parabola-shaped curve opening upward, crossing a horizontal axis "
        "line at two distinct points, with a vertical axis line to the left "
        "forming a plain right-angle corner with the horizontal line (a bare "
        "coordinate-axis corner, no numbers, no arrowheads), and a thin dashed "
        "vertical line dropping from the curve's lowest point down to the "
        "horizontal axis exactly between the two crossing points. Clean "
        "mathematical textbook diagram, no text, no numbers"
    )),
]

async def main():
    settings = get_settings()
    print("image_provider:", settings.image_provider, "| mock:", settings.use_mock_ai)
    assert not settings.use_mock_ai
    assert settings.image_provider == "azure"

    template = load_template("technical_v1")
    ai = get_ai_client()

    for name, prompt_text in CASES:
        block = Block(type="image", content={"illustration_style": "", "prompt": prompt_text})
        prompt = ImageService.build_prompt(block, template, "Validation Course")
        print(f"\n=== {name} === (final prompt len={len(prompt)} chars)")
        print(prompt_text)
        t0 = time.time()
        data = None
        try:
            data = await ai.image(prompt=prompt, size=settings.image_size)
        except Exception as exc:
            print(f"{name} FAILED:", type(exc).__name__, str(exc)[:300])
        t1 = time.time()
        print(f"{name}: {t1-t0:.1f}s, bytes={len(data) if data else 0}")
        if data:
            with open(f".scratch_validate4/{name}.png", "wb") as f:
                f.write(data)

asyncio.run(main())
