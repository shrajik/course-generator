"""Vision-model quality gate for a Wikimedia Commons candidate image.

`app.services.wikimedia_provider.search_commons` and its own deterministic
license/resolution filter only establish that a candidate is LEGALLY reusable
and technically well-formed - neither can tell whether it actually depicts
the requested topic, reads as a genuine textbook-style diagram rather than a
decorative photo, carries only English labels, or is scientifically sound.
That requires real image understanding, so - mirroring
app.services.diagram_content_qa's own "Layer 0 deterministic, Layer 1 AI
judgment" shape, and reusing the EXACT same `ai.structured(..., image=...)`
call shape `ImageService._verify_image_relevance` already uses (no new
AI-call plumbing) - this module is that one additional judgment call.

Same honest-provenance contract every other vision check in this codebase
already keeps (see ImageRelevanceCheck's own docstring): every field
defaults to True so a vision-call FAILURE never masquerades as a CONFIRMED
defect the candidate never earned, and `errored` separately records that the
check could not actually run at all - a caller can tell "genuinely verified
clean" from "never verified" instead of conflating the two."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.services.openai_service import AIClient, get_ai_client

log = get_logger(__name__)


class WikimediaCandidateCheck(BaseModel):
    """A vision-capable model's judgment on one downloaded Commons
    candidate - see this module's own docstring for why each field
    defaults to True and `errored` is tracked separately."""

    model_config = ConfigDict(extra="ignore")

    on_topic: bool = True
    on_topic_detail: str = ""
    # Prefer a clean, structured, textbook-style diagram/illustration over a
    # photograph, decorative stock image or generic graphic - the request's
    # own explicit preference (see ImageService._try_wikimedia_image).
    textbook_style: bool = True
    textbook_style_detail: str = ""
    # True by default so an image with NO text at all (nothing to be in the
    # wrong language) never fails this for free - same "nothing to collide"
    # reasoning ImageRelevanceCheck.labels_legible already uses.
    english_labels_only: bool = True
    english_labels_detail: str = ""
    scientifically_accurate: bool = True
    scientifically_accurate_detail: str = ""
    errored: bool = False


CANDIDATE_CHECK_SYSTEM = """\
You are a strict reviewer deciding whether a candidate image found on \
Wikimedia Commons is suitable to use, AS IS, as an educational illustration \
in a course. Look at the attached image and judge four things:

- `on_topic`: does the image's actual visual content genuinely, specifically \
depict the stated brief - not a generic, unrelated, or merely thematically- \
adjacent picture?
- `textbook_style`: does this read as a clean, structured, professionally- \
presented educational diagram or illustration - the kind found in a real \
textbook - rather than a casual photograph, decorative stock image, meme, \
icon, or generic/abstract graphic? Prefer a labelled diagram, cross-section, \
schematic or clear illustrative photograph of the actual subject over \
anything decorative or only loosely thematic.
- `english_labels_only`: if the image has ANY text, labels or captions baked \
into it, is every one of them in English? An image containing Hindi, or any \
other non-English script, in its labels FAILS this even if otherwise \
perfect. An image with no text at all passes this automatically - there is \
nothing to check.
- `scientifically_accurate`: so far as you can tell, is the image free of \
obvious scientific, technical or factual errors in what it depicts or \
labels?

Be concrete in every detail field: name exactly what the image shows and \
why it does or doesn't meet each bar, never a bare "could be better". If the \
image is genuinely good, say so plainly - do not manufacture a problem."""


async def evaluate_candidate(
    image: bytes,
    *,
    brief: str,
    course_title: str,
    ai: AIClient | None = None,
    settings: Settings | None = None,
) -> WikimediaCandidateCheck:
    """One vision call per candidate (the caller - ImageService - is
    responsible for stopping at the first PASS and capping how many
    candidates ever reach this, per the "avoid unnecessary sequential AI
    calls" requirement). Never raises - a vision-check failure returns
    `errored=True`, never a confirmed-bad verdict the candidate never
    earned (same contract as `ImageService._verify_image_relevance`)."""
    if not brief.strip():
        return WikimediaCandidateCheck()
    try:
        return await (ai or get_ai_client()).structured(
            schema=WikimediaCandidateCheck,
            system=CANDIDATE_CHECK_SYSTEM,
            user=f"COURSE: {course_title}\n\nBRIEF (what this image is supposed to depict):\n{brief}\n\n"
            "Judge the attached candidate image against this brief.",
            model=(settings or get_settings()).diagram_model,
            purpose="wikimedia_candidate_check",
            phase="image",
            image=image,
        )
    except Exception as exc:  # noqa: BLE001 - never block course generation on a QA-step failure
        log.warning("Wikimedia candidate check failed (inconclusive, never treated as a pass): %s", exc)
        return WikimediaCandidateCheck(errored=True)


def candidate_passed(check: WikimediaCandidateCheck) -> bool:
    """A single, reusable pass/fail decision - mirrors the local
    `_relevance_failed` helper `ImageService._generate_illustration` already
    uses for `ImageRelevanceCheck`. `errored` is checked explicitly so an
    exception never silently passes through as "verified good"."""
    return not check.errored and (
        check.on_topic
        and check.textbook_style
        and check.english_labels_only
        and check.scientifically_accurate
    )
