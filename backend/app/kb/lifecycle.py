"""The rules of an item's lifecycle, and of switching its live version.

`lifecycle_status` is the admin's decision about the item (draft, published,
unpublished, archived). It is separate from a version's processing status and from
a job's status: a version can be fully processed and still not be live, and only an
explicit admin action ever makes it so.

One invariant carries the whole design: a passage is `is_live` exactly when its item
is published and neither deleted nor purged, and it belongs to the item's live
version. Retrieval relies on that and nothing else (see app/services/knowledge_pg.py),
so every transition here says whether the item ends up live.

Pure functions: they decide, the service applies the decision in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.errors import ConflictError

Action = Literal["publish", "unpublish", "archive", "restore_archived", "delete", "restore_deleted", "purge"]
PublishResult = Literal["noop", "publish", "switch"]


@dataclass(frozen=True)
class ItemState:
    lifecycle: str
    deleted: bool = False
    purged: bool = False
    # The item has been published at some point (it has a live version pointer).
    has_live_version: bool = False


@dataclass(frozen=True)
class Transition:
    lifecycle: str
    deleted: bool
    live: bool  # whether the item's live version's passages are live afterwards
    purged: bool = False


def is_live(lifecycle: str, *, deleted: bool = False, purged: bool = False) -> bool:
    """The invariant retrieval depends on."""
    return lifecycle == "published" and not deleted and not purged


def apply(state: ItemState, action: Action, *, version_ready: bool = True, purge_allowed: bool = False) -> Transition:
    """The state after `action`, or a ConflictError saying why it cannot happen."""
    if state.purged:
        raise ConflictError("This item has been permanently removed.")
    if state.deleted and action not in ("restore_deleted", "purge"):
        raise ConflictError("This item is in the trash. Restore it first.")

    def done(lifecycle: str, *, deleted: bool | None = None, purged: bool = False) -> Transition:
        gone = state.deleted if deleted is None else deleted
        return Transition(lifecycle, gone, is_live(lifecycle, deleted=gone, purged=purged), purged)

    if action == "publish":
        if state.lifecycle == "published":
            raise ConflictError("This item is already published.")
        if state.lifecycle == "archived":
            raise ConflictError("Restore this archived item before publishing it.")
        if not version_ready:
            raise ConflictError("The item has no fully processed version to publish yet.")
        return done("published")
    if action == "unpublish":
        if state.lifecycle != "published":
            raise ConflictError("Only a published item can be unpublished.")
        return done("unpublished")
    if action == "archive":
        if state.lifecycle == "archived":
            raise ConflictError("This item is already archived.")
        return done("archived")
    if action == "restore_archived":
        if state.lifecycle != "archived":
            raise ConflictError("This item is not archived.")
        # Never straight back to live: an admin publishes it again on purpose.
        return done("unpublished" if state.has_live_version else "draft")
    if action == "delete":
        return done(state.lifecycle, deleted=True)
    if action == "restore_deleted":
        if not state.deleted:
            raise ConflictError("This item is not in the trash.")
        return done("unpublished" if state.lifecycle == "published" else state.lifecycle, deleted=False)
    if action == "purge":
        if not state.deleted:
            raise ConflictError("Only an item in the trash can be permanently removed.")
        if not purge_allowed:
            raise ConflictError("Permanent removal is not enabled yet.")
        return done(state.lifecycle, deleted=True, purged=True)
    raise ConflictError(f"Unknown action {action!r}.")


def check_publish_version(
    state: ItemState,
    *,
    version_id: object,
    version_item_id: object,
    item_id: object,
    version_ready: bool,
    version_chunk_count: int,
    live_version_id: object | None,
    expected_live_version_id: object | None,
) -> PublishResult:
    """Whether making `version_id` the live version is allowed, and what kind of change it is.

    `expected_live_version_id` is what the admin was looking at when they clicked: if
    someone else has switched versions since, this is a conflict (409), not a silent
    overwrite. Returns `noop` (already live), `publish` (the item was not live) or
    `switch` (replacing another live version).
    """
    if state.purged or state.deleted:
        raise ConflictError("This item has been deleted.")
    if state.lifecycle == "archived":
        raise ConflictError("Restore this archived item before publishing a version.")
    if version_item_id != item_id:
        raise ConflictError("That version belongs to a different item.")
    if live_version_id != expected_live_version_id:
        raise ConflictError("The live version changed since you opened this item. Reload and try again.")
    if state.lifecycle == "published" and live_version_id == version_id:
        return "noop"
    if not version_ready:
        raise ConflictError("That version has not finished processing.")
    if version_chunk_count <= 0:
        raise ConflictError("That version has no content to publish.")
    return "switch" if state.lifecycle == "published" else "publish"


def liveness_changes(old_chunk_ids: set, new_chunk_ids: set) -> tuple[set, set]:
    """(to switch off, to switch on) when the live version goes from one set of passages
    to another. A passage both versions share is never touched, so there is no moment
    at which it is not live."""
    return old_chunk_ids - new_chunk_ids, new_chunk_ids - old_chunk_ids
