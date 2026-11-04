"""The knowledge-base write-path rules: lineage, retry, lifecycle, classification."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.core.errors import ConflictError, ValidationFailedError
from app.ingestion.parsers import IngestionError
from app.kb import classify, lifecycle, retry
from app.kb.lineage import ExistingChunk, IncomingChunk, plan_lineage

MODEL = "text-embedding-3-small"


# --- lineage -----------------------------------------------------------------------------


def _chunk(ordinal: int, text: str, part: str | None = "a", *, embedded=True, model=MODEL) -> ExistingChunk:
    return ExistingChunk(id=f"c{ordinal}", ordinal=ordinal, embed_hash=f"h:{text}", part=part, embed_model=model if embedded else None, has_embedding=embedded)


def _incoming(text: str, part: str | None = "a") -> IncomingChunk:
    return IncomingChunk(embed_hash=f"h:{text}", part=part, payload=text)


def test_an_identical_reupload_reuses_everything_and_embeds_nothing():
    previous = [_chunk(1, "x"), _chunk(2, "y"), _chunk(3, "z")]
    plan = plan_lineage(previous, [_incoming(t) for t in "xyz"], next_ordinal=4, embed_model=MODEL)
    assert [c.ordinal for c in plan.chunks] == [1, 2, 3]
    assert (plan.reused, plan.created, plan.dropped, plan.to_embed, plan.next_ordinal) == (3, 0, [], [], 4)


def test_an_edit_makes_a_new_ordinal_for_the_edited_passage_only():
    previous = [_chunk(1, "x"), _chunk(2, "y"), _chunk(3, "z")]
    plan = plan_lineage(previous, [_incoming("x"), _incoming("y EDITED"), _incoming("z")], next_ordinal=4, embed_model=MODEL)
    assert [c.ordinal for c in plan.chunks] == [1, 4, 3]
    assert [c.payload for c in (p.incoming for p in plan.to_embed)] == ["y EDITED"]
    assert [d.ordinal for d in plan.dropped] == [2] and plan.next_ordinal == 5  # 2 is retired, never reissued


def test_moving_and_removing_passages_keeps_the_ordinals_of_those_that_remain():
    previous = [_chunk(1, "x"), _chunk(2, "y"), _chunk(3, "z")]
    plan = plan_lineage(previous, [_incoming("z"), _incoming("x")], next_ordinal=4, embed_model=MODEL)
    assert [c.ordinal for c in plan.chunks] == [3, 1]
    assert [d.ordinal for d in plan.dropped] == [2]


def test_a_passage_added_in_the_middle_does_not_renumber_the_ones_after_it():
    previous = [_chunk(1, "x"), _chunk(2, "z")]
    plan = plan_lineage(previous, [_incoming("x"), _incoming("new"), _incoming("z")], next_ordinal=3, embed_model=MODEL)
    assert [c.ordinal for c in plan.chunks] == [1, 3, 2]


def test_reverting_an_edit_restores_the_original_citation():
    history = [_chunk(2, "y")]  # an earlier version had it; the previous one does not
    previous = [_chunk(1, "x"), _chunk(4, "y EDITED")]
    plan = plan_lineage(previous, [_incoming("x"), _incoming("y")], next_ordinal=5, history=history, embed_model=MODEL)
    assert [(c.ordinal, c.origin) for c in plan.chunks] == [(1, "previous"), (2, "history")]
    assert [d.ordinal for d in plan.dropped] == [4]


def test_replacing_a_file_with_its_updated_version_keeps_the_citations_of_unchanged_passages():
    # The file's hash (its "part") changes with every edit; the passages that did not change must not.
    previous = [_chunk(1, "x", part="old-file"), _chunk(2, "y", part="old-file")]
    plan = plan_lineage(previous, [_incoming("x", part="new-file"), _incoming("y EDITED", part="new-file")], next_ordinal=3, embed_model=MODEL)
    assert [c.ordinal for c in plan.chunks] == [1, 3] and plan.reused == 1


def test_identical_text_in_two_files_pairs_each_with_its_own_file_first():
    previous = [_chunk(1, "same", part="a"), _chunk(2, "same", part="b")]
    plan = plan_lineage(previous, [_incoming("same", part="b"), _incoming("same", part="a")], next_ordinal=3, embed_model=MODEL)
    assert [c.ordinal for c in plan.chunks] == [2, 1]


def test_duplicate_passages_pair_off_one_to_one():
    previous = [_chunk(1, "same"), _chunk(2, "same")]
    plan = plan_lineage(previous, [_incoming("same"), _incoming("same"), _incoming("same")], next_ordinal=3, embed_model=MODEL)
    assert [c.ordinal for c in plan.chunks] == [1, 2, 3] and plan.created == 1


def test_a_reused_passage_without_a_vector_for_this_model_is_embedded_again():
    previous = [_chunk(1, "x", embedded=False), _chunk(2, "y", model="old-model"), _chunk(3, "z")]
    plan = plan_lineage(previous, [_incoming(t) for t in "xyz"], next_ordinal=4, embed_model=MODEL)
    assert [c.ordinal for c in plan.to_embed] == [1, 2]  # a retry fills exactly the gaps


def test_the_counter_must_be_above_every_ordinal_ever_used():
    with pytest.raises(ValueError):
        plan_lineage([_chunk(5, "x")], [], next_ordinal=5)
    with pytest.raises(ValueError):
        plan_lineage([], [], next_ordinal=3, history=[_chunk(9, "x")])


def test_whatever_the_edits_ordinals_are_unique_and_never_change_meaning():
    """Randomised: many generations of edits; an ordinal must always mean one text."""
    rng = random.Random(7)
    meaning: dict[int, str] = {}  # ordinal -> the one text it has ever meant
    current: list[ExistingChunk] = []
    history: dict[int, ExistingChunk] = {}
    next_ordinal = 1
    words = [f"w{i}" for i in range(12)]
    for _ in range(60):
        texts = [rng.choice(words) for _ in range(rng.randint(0, 8))]
        plan = plan_lineage(current, [_incoming(t) for t in texts], next_ordinal=next_ordinal, history=[h for o, h in history.items() if o not in {c.ordinal for c in current}], embed_model=MODEL)
        ordinals = [c.ordinal for c in plan.chunks]
        assert len(ordinals) == len(set(ordinals))
        for planned in plan.chunks:
            text = planned.incoming.payload
            assert meaning.setdefault(planned.ordinal, text) == text
        next_ordinal = plan.next_ordinal
        current = [ExistingChunk(f"c{p.ordinal}", p.ordinal, p.incoming.embed_hash, p.incoming.part, MODEL, True) for p in plan.chunks]
        history.update({c.ordinal: c for c in current})
        assert next_ordinal > max(meaning, default=0)


# --- retry --------------------------------------------------------------------------------


def test_errors_are_classified_permanent_or_transient():
    for permanent in (IngestionError("scanned"), ValidationFailedError("bad"), ConflictError("x")):
        assert retry.classify_error(permanent) == "permanent"
    for transient in (TimeoutError(), ConnectionError(), RuntimeError("provider"), retry.EmbeddingIncomplete()):
        assert retry.classify_error(transient) == "transient"


def test_backoff_doubles_then_caps_with_bounded_jitter():
    rng = random.Random(1)
    delays = [retry.backoff_seconds(n, rng=rng) for n in range(1, 12)]
    for attempt, delay in zip(range(1, 12), delays):
        base = min(retry.MAX_DELAY_SECONDS, retry.BASE_DELAY_SECONDS * 2 ** (attempt - 1))
        assert base <= delay <= base * 1.2 + 0.001
    assert max(delays) <= retry.MAX_DELAY_SECONDS * 1.2


def test_decisions_follow_the_policy():
    transient, permanent = RuntimeError("down"), IngestionError("scanned")
    assert retry.decide(permanent, attempts=1, max_attempts=5).action == "fail"
    assert retry.decide(permanent, attempts=5, max_attempts=5, degradable=True).action == "fail"
    first = retry.decide(transient, attempts=1, max_attempts=5)
    assert first.action == "retry" and first.delay_seconds >= retry.BASE_DELAY_SECONDS
    assert retry.decide(transient, attempts=4, max_attempts=5).action == "retry"
    assert retry.decide(transient, attempts=5, max_attempts=5).action == "dead"
    assert retry.decide(transient, attempts=5, max_attempts=5, degradable=True).action == "degrade"


def test_a_lease_expires_only_after_the_heartbeat_goes_quiet():
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert retry.lease_expired(None, now)
    assert not retry.lease_expired(now - timedelta(seconds=30), now)
    assert retry.lease_expired(now - timedelta(seconds=retry.LEASE_SECONDS + 1), now)


# --- lifecycle ----------------------------------------------------------------------------


def _state(lifecycle_name="draft", **kw) -> lifecycle.ItemState:
    return lifecycle.ItemState(lifecycle_name, **kw)


def test_publish_unpublish_archive_and_restore_follow_the_rules():
    published = lifecycle.apply(_state("draft"), "publish")
    assert (published.lifecycle, published.live) == ("published", True)
    assert lifecycle.apply(_state("published"), "unpublish") == lifecycle.Transition("unpublished", False, False)
    assert lifecycle.apply(_state("unpublished"), "publish").live is True
    for start in ("draft", "published", "unpublished"):
        assert lifecycle.apply(_state(start), "archive") == lifecycle.Transition("archived", False, False)
    # Restoring an archived item never makes it live on its own.
    assert lifecycle.apply(_state("archived", has_live_version=True), "restore_archived").lifecycle == "unpublished"
    assert lifecycle.apply(_state("archived", has_live_version=False), "restore_archived").lifecycle == "draft"
    assert not lifecycle.apply(_state("archived", has_live_version=True), "restore_archived").live


@pytest.mark.parametrize(
    ("state", "action"),
    [
        (_state("published"), "publish"),
        (_state("draft"), "unpublish"),
        (_state("archived"), "publish"),
        (_state("archived"), "archive"),
        (_state("draft"), "restore_archived"),
        (_state("draft"), "restore_deleted"),
        (_state("draft", deleted=True), "publish"),
        (_state("published", deleted=True), "unpublish"),
        (_state("draft"), "purge"),
        (_state("draft", deleted=True, purged=True), "restore_deleted"),
        (_state("draft", deleted=True, purged=True), "purge"),
    ],
)
def test_impossible_transitions_are_conflicts(state, action):
    with pytest.raises(ConflictError):
        lifecycle.apply(state, action, purge_allowed=True)


def test_publishing_needs_a_processed_version_and_deleting_always_takes_an_item_out_of_retrieval():
    with pytest.raises(ConflictError):
        lifecycle.apply(_state("draft"), "publish", version_ready=False)
    for start in ("draft", "published", "unpublished", "archived"):
        gone = lifecycle.apply(_state(start), "delete")
        assert gone.deleted and not gone.live and gone.lifecycle == start


def test_restoring_from_the_trash_never_goes_live_and_purge_needs_the_flag():
    back = lifecycle.apply(_state("published", deleted=True), "restore_deleted")
    assert (back.lifecycle, back.deleted, back.live) == ("unpublished", False, False)
    assert lifecycle.apply(_state("draft", deleted=True), "restore_deleted").lifecycle == "draft"
    with pytest.raises(ConflictError, match="not enabled"):
        lifecycle.apply(_state("draft", deleted=True), "purge")
    purged = lifecycle.apply(_state("draft", deleted=True), "purge", purge_allowed=True)
    assert purged.purged and not purged.live


def test_the_live_invariant_holds_after_every_possible_transition():
    """Walk every action from every state: an item is live iff published and not deleted/purged."""
    states = [_state(l, deleted=d) for l in ("draft", "published", "unpublished", "archived") for d in (False, True)]
    actions = ("publish", "unpublish", "archive", "restore_archived", "delete", "restore_deleted", "purge")
    for state in states:
        for action in actions:
            try:
                result = lifecycle.apply(state, action, purge_allowed=True)
            except ConflictError:
                continue
            assert result.live == lifecycle.is_live(result.lifecycle, deleted=result.deleted, purged=result.purged)


def _publish(state, **overrides):
    args = dict(version_id="v2", version_item_id="i1", item_id="i1", version_ready=True, version_chunk_count=3, live_version_id="v1", expected_live_version_id="v1")
    args.update(overrides)
    return lifecycle.check_publish_version(state, **args)


def test_switching_versions_is_checked_for_staleness_readiness_and_ownership():
    assert _publish(_state("published")) == "switch"
    assert _publish(_state("unpublished", has_live_version=True)) == "publish"
    assert _publish(_state("draft"), live_version_id=None, expected_live_version_id=None) == "publish"
    assert _publish(_state("published"), version_id="v1") == "noop"
    for bad in (
        dict(expected_live_version_id="v0"),  # someone else switched since the admin looked
        dict(version_ready=False),
        dict(version_chunk_count=0),
        dict(version_item_id="other"),
    ):
        with pytest.raises(ConflictError):
            _publish(_state("published"), **bad)
    for state in (_state("archived"), _state("published", deleted=True), _state("published", purged=True)):
        with pytest.raises(ConflictError):
            _publish(state)


def test_a_version_switch_never_touches_passages_both_versions_share():
    off, on = lifecycle.liveness_changes({"a", "b", "c"}, {"b", "c", "d"})
    assert (off, on) == ({"a"}, {"d"})
    assert lifecycle.liveness_changes(set(), {"a"}) == (set(), {"a"})  # first publish
    assert lifecycle.liveness_changes({"a"}, {"a"}) == (set(), set())


# --- classification -----------------------------------------------------------------------


def _vec(*values: float) -> list[float]:
    return list(values)


CATS = [
    classify.CategoryCentroid("c-hr", "HR policy", _vec(1, 0, 0), 5),
    classify.CategoryCentroid("c-sec", "Security", _vec(0, 1, 0), 3),
    classify.CategoryCentroid("c-cook", "Cooking", _vec(0, 0, 1), 2),
]


def test_a_clear_match_is_assigned_with_confidence_and_a_vague_one_goes_to_review():
    clear = classify.match_category(_vec(0.99, 0.05, 0.02), CATS)
    assert (clear.status, clear.category_id, clear.name) == ("assigned", "c-hr", "HR policy") and clear.confidence > 0.9
    between = classify.match_category(_vec(1, 1, 0), CATS)  # equally close to two
    assert between.status == "needs_review" and {between.category_id, between.alternatives[0]["id"]} == {"c-hr", "c-sec"}
    far = classify.match_category(_vec(0.4, 0.4, 0.4), CATS, candidate_similarity=0.9)
    assert far.status == "none"


def test_no_categories_no_vector_or_empty_categories_mean_no_match():
    assert classify.match_category(_vec(1, 0, 0), []).status == "none"
    assert classify.match_category(None, CATS).status == "none"
    empty = [classify.CategoryCentroid("c", "Empty", _vec(1, 0, 0), 0)]
    assert classify.match_category(_vec(1, 0, 0), empty).status == "none"


def test_sections_get_topic_labels_only_when_close_enough():
    labels = classify.label_sections([_vec(1, 0.1, 0), _vec(0, 0, 1), _vec(0.1, 0.1, 0.1), None, [0, 0, 0]], CATS, threshold=0.7)
    assert labels == [["HR policy"], ["Cooking"], [], [], []]
    assert classify.label_sections([_vec(1, 0, 0)], [], threshold=0.1) == [[]]
    assert classify.label_sections([_vec(1, 1, 0)], CATS, threshold=0.5, max_labels=1) in ([["HR policy"]], [["Security"]])


def test_category_centroids_update_incrementally_and_reverse():
    centroid, count = None, 0
    for v in ([1, 0, 0], [0, 1, 0]):
        centroid, count = classify.update_centroid(centroid, count, v)
    assert count == 2 and np.allclose(centroid, [0.5, 0.5, 0])
    centroid, count = classify.update_centroid(centroid, count, [0, 1, 0], remove=True)
    assert count == 1 and np.allclose(centroid, [1, 0, 0])
    assert classify.update_centroid(centroid, count, [1, 0, 0], remove=True) == (None, 0)
    assert classify.update_centroid([1, 0, 0], 1, [0, 0, 0]) == ([1, 0, 0], 1)  # a zero vector changes nothing
    assert classify.mean_vector([None, [0, 0, 0]]) is None
    assert np.allclose(classify.mean_vector([[3, 0], [0, 5]]), [0.5, 0.5])


def test_titles_come_from_the_documents_own_heading_else_the_filename():
    assert classify.suggest_title("x.pdf", ["REFUND POLICY", "  1.1 Eligibility"]) == "REFUND POLICY"
    assert classify.suggest_title("refund_policy-v2.pdf", ["  1.1 Eligibility", "ab"]) == "refund policy v2"
    assert classify.suggest_title("", [], "Some opening words of the text") == "Some opening words of the text"
    assert classify.suggest_title("", []) == "Untitled"


def test_key_terms_prefer_repeated_phrases_and_skip_noise():
    text = [
        "Refund requests must be approved by the billing team. The refund request form is on the intranet.",
        "A refund request is escalated to the billing team within two days. Page 12 of 40.",
    ]
    terms = dict(classify.key_terms(text))
    assert "refund request" in terms and "billing team" in terms
    assert all(not name.isdigit() for name in terms) and "the" not in terms and max(terms.values()) == 1.0
    assert "refund" not in terms  # covered by the phrase
    assert classify.key_terms([]) == []


def test_the_model_response_is_cleaned_and_bounded():
    raw = classify.MetadataSuggestion(title="  A   very\nlong " + "x" * 300, description="d " * 400, tags=["Refunds!", "refunds", "  Billing   Team ", "x", "a" * 60, *[f"t{i}" for i in range(20)]])
    clean = classify.clean_metadata(raw)
    assert len(clean.title) <= 200 and clean.title.startswith("A very long")
    assert len(clean.description) <= 500
    assert clean.tags[:2] == ["refunds", "billing team"] and len(clean.tags) == 8 and "x" not in clean.tags


def test_the_prompt_samples_across_the_document_and_lists_existing_tags():
    chunks = [f"section {i} " + "word " * 300 for i in range(20)]
    system, user = classify.build_metadata_prompt("policy.pdf", ["Intro", "Refunds"], chunks, ["refunds", "billing"])
    assert "knowledge base" in system and "Filename: policy.pdf" in user and "Existing tags: refunds, billing" in user
    sections = {int(n) for n in __import__("re").findall(r"section (\d+)", user)}
    assert 0 in sections and max(sections) >= 10  # drawn from across the document, not only its opening
    assert len(user.split()) < classify.PROMPT_SAMPLE_WORDS + 250
    assert classify.sample_text([]) == "" and classify.sample_text(["short text"]) == "short text"


def test_suggestions_use_the_model_when_present_and_the_rules_when_not():
    chunks = ["Refund request handling. The refund request goes to the billing team.", "Another refund request and billing team note."]
    assigned = classify.CategoryMatch("assigned", "c-hr", "HR policy", 0.8, 0.9)
    model = classify.MetadataSuggestion(title="Refund handling", description="How refunds are handled.", tags=["Refunds"])

    suggestions, review = classify.assemble_suggestions(filename="policy.pdf", outline=[], chunks=chunks, category=assigned, model=model)
    assert suggestions["generated_by"] == "model" and suggestions["title"] == "Refund handling"
    assert suggestions["tags"] == [{"name": "refunds", "confidence": 0.8, "source": "ai"}]
    assert suggestions["category"]["id"] == "c-hr" and review is False  # confident category, has a description

    rules, review = classify.assemble_suggestions(filename="refund_policy.pdf", outline=[], chunks=chunks, category=classify.CategoryMatch("none"))
    assert rules["generated_by"] == "rules" and rules["title"] == "refund policy" and rules["description"] == ""
    assert rules["category"] is None and review is True and all(t["source"] == "rules" for t in rules["tags"])
    # A model that returned nothing useful falls back to the rules.
    empty, _ = classify.assemble_suggestions(filename="a.pdf", outline=[], chunks=chunks, category=assigned, model=classify.MetadataSuggestion())
    assert empty["generated_by"] == "rules"
