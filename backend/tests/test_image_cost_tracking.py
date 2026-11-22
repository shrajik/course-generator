"""OpenAIClient._megapixels_for - the real-resolution-to-megapixels
conversion that feeds app.core.pricing's FLUX.2-flex cost estimate (see
that module's docstring). Matches Azure's own documented billing rule:
round UP to the next whole megapixel, 1 megapixel == 1024x1024.
"""

from __future__ import annotations

from app.services.openai_service import OpenAIClient


def test_exactly_one_megapixel_rounds_to_one():
    assert OpenAIClient._megapixels_for("1024x1024") == 1.0


def test_a_larger_resolution_rounds_up_to_the_next_whole_megapixel():
    # 1536*1024 == 1,572,864 pixels == 1.5 megapixels -> rounds up to 2.
    assert OpenAIClient._megapixels_for("1536x1024") == 2.0


def test_a_smaller_resolution_still_rounds_up_to_at_least_one():
    assert OpenAIClient._megapixels_for("512x512") == 1.0


def test_missing_or_unparseable_size_is_zero_not_a_guess():
    assert OpenAIClient._megapixels_for(None) == 0.0
    assert OpenAIClient._megapixels_for("") == 0.0
    assert OpenAIClient._megapixels_for("not-a-size") == 0.0
    assert OpenAIClient._megapixels_for("1024") == 0.0
