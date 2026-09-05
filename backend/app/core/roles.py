"""Platform roles and the high-level permissions the FRD assigns to each.

Centralised here so authorization checks are never duplicated ad hoc across
endpoints - a route depends on `require_roles(...)` (see `api/dependencies.py`)
instead of comparing `user.role` strings inline.

`ROLE_PERMISSIONS` documents the FRD's high-level permission grants per role.
It is descriptive, not yet enforced: this codebase has no draft/review-queue
content model to attach fine-grained content permissions to, so wiring
`has_permission()` into specific endpoints is left for when those features
land, rather than invented here.
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    AUTHOR = "author"
    EDITOR_REVIEWER = "editor_reviewer"
    MANAGER = "manager"
    ADMIN = "admin"


DEFAULT_ROLE = Role.AUTHOR

ROLE_VALUES: tuple[str, ...] = tuple(role.value for role in Role)


class Permission(str, Enum):
    CREATE_CONTENT = "create_content"
    EDIT_OWN_DRAFT = "edit_own_draft"
    COMMENT_OWN_CONTENT = "comment_own_content"
    EXPORT_OWN_DRAFT = "export_own_draft"

    VIEW_REVIEW_QUEUE = "view_review_queue"
    VIEW_PUBLISHED_CONTENT = "view_published_content"
    EDIT_CONTENT_UNDER_REVIEW = "edit_content_under_review"
    COMMENT_ANY_CONTENT = "comment_any_content"
    APPROVE_REJECT_CONTENT = "approve_reject_content"
    EXPORT_APPROVED_CONTENT = "export_approved_content"

    VIEW_PROJECTS_AND_REPORTS = "view_projects_and_reports"
    COMMENT_FOR_COORDINATION = "comment_for_coordination"
    MANAGE_PROJECT_SCHEDULE = "manage_project_schedule"

    FULL_PLATFORM_VISIBILITY = "full_platform_visibility"
    MANAGE_USERS_AND_ROLES = "manage_users_and_roles"
    MANAGE_TEMPLATES_AND_KNOWLEDGE_BASE = "manage_templates_and_knowledge_base"
    MANAGE_SYSTEM_CONFIGURATION = "manage_system_configuration"
    EXPORT_ANY_CONTENT = "export_any_content"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.AUTHOR: frozenset(
        {
            Permission.CREATE_CONTENT,
            Permission.EDIT_OWN_DRAFT,
            Permission.COMMENT_OWN_CONTENT,
            Permission.EXPORT_OWN_DRAFT,
        }
    ),
    Role.EDITOR_REVIEWER: frozenset(
        {
            Permission.VIEW_REVIEW_QUEUE,
            Permission.VIEW_PUBLISHED_CONTENT,
            Permission.EDIT_CONTENT_UNDER_REVIEW,
            Permission.COMMENT_ANY_CONTENT,
            Permission.APPROVE_REJECT_CONTENT,
            Permission.EXPORT_APPROVED_CONTENT,
        }
    ),
    Role.MANAGER: frozenset(
        {
            Permission.VIEW_PROJECTS_AND_REPORTS,
            Permission.VIEW_PUBLISHED_CONTENT,
            Permission.COMMENT_FOR_COORDINATION,
            Permission.MANAGE_PROJECT_SCHEDULE,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Permission.FULL_PLATFORM_VISIBILITY,
            Permission.MANAGE_USERS_AND_ROLES,
            Permission.MANAGE_TEMPLATES_AND_KNOWLEDGE_BASE,
            Permission.MANAGE_SYSTEM_CONFIGURATION,
            Permission.EXPORT_ANY_CONTENT,
        }
    ),
}


def has_permission(role: str | Role, permission: Permission) -> bool:
    try:
        resolved = Role(role)
    except ValueError:
        return False
    return permission in ROLE_PERMISSIONS.get(resolved, frozenset())
