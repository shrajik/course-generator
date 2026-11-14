"""PDF export requires final SME approval for author/editor roles.

A direct unit test of `user_can_export_pdf`/`check_document_owner_for_pdf_export`
(app.api.course_authorization), independent of the TEST_DATABASE_URL-gated
integration suite (test_course_ownership.py, test_authorization_audit.py)
which also exercises this through the real HTTP routes but only runs where a
real Postgres test database is configured.
"""

from __future__ import annotations

import pytest

from app.api.course_authorization import check_document_owner_for_pdf_export, user_can_export_pdf
from app.core.errors import ForbiddenError
from app.core.roles import Role
from app.db.models import User
from app.schemas.course import CourseInput, CourseRecord


def _user(role: Role, owner_id: str = "user-1") -> User:
    return User(id=owner_id, email="u@example.com", password_hash="x", role=role.value, is_active=True, is_verified=True)


def _record(review_status: str, owner_id: str = "user-1") -> CourseRecord:
    return CourseRecord(
        course_id="crs_test",
        document_id="doc_test",
        input=CourseInput(course_title="T", toc=[{"title": "A"}], target_audience="x", template="technical"),
        template_id="technical_v1",
        owner_id=owner_id,
        review_status=review_status,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )


@pytest.mark.parametrize("review_status", ["draft", "in_review", "changes_requested"])
def test_an_author_cannot_export_pdf_before_approval(review_status):
    assert user_can_export_pdf(_user(Role.AUTHOR), _record(review_status)) is False


def test_an_author_can_export_pdf_once_approved():
    assert user_can_export_pdf(_user(Role.AUTHOR), _record("approved")) is True


@pytest.mark.parametrize("review_status", ["draft", "in_review", "changes_requested"])
def test_an_editor_reviewer_cannot_export_pdf_before_approval(review_status):
    assert user_can_export_pdf(_user(Role.EDITOR_REVIEWER), _record(review_status)) is False


def test_an_editor_reviewer_can_export_pdf_once_approved():
    assert user_can_export_pdf(_user(Role.EDITOR_REVIEWER), _record("approved")) is True


@pytest.mark.parametrize("review_status", ["draft", "in_review", "changes_requested", "approved"])
def test_an_admin_can_always_export_pdf(review_status):
    assert user_can_export_pdf(_user(Role.ADMIN), _record(review_status)) is True


async def test_the_export_route_guard_rejects_an_unapproved_authors_own_course(monkeypatch):
    class _Service:
        def resolve_course_id(self, document_id: str) -> str:
            return "crs_test"

    class _CourseService:
        async def get_course_record(self, course_id: str) -> CourseRecord:
            return _record("in_review")

    with pytest.raises(ForbiddenError):
        await check_document_owner_for_pdf_export(
            "doc_test", _user(Role.AUTHOR), _CourseService(), _Service()
        )


async def test_the_export_route_guard_allows_the_same_course_once_approved(monkeypatch):
    class _Service:
        def resolve_course_id(self, document_id: str) -> str:
            return "crs_test"

    class _CourseService:
        async def get_course_record(self, course_id: str) -> CourseRecord:
            return _record("approved")

    course_id = await check_document_owner_for_pdf_export(
        "doc_test", _user(Role.AUTHOR), _CourseService(), _Service()
    )
    assert course_id == "crs_test"
