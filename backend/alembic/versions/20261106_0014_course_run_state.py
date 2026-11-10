"""Add courses.run_state - an indexed mirror of CourseRecord.run.state.

Additive only. Lets the crash-recovery startup scan find every course whose
generation run was "running"/"queued" when the process died with one indexed
query, instead of deserializing every course's metadata_json. Nullable:
existing rows (and any course that has never been generated) simply have no
run_state until their next save.

Revision ID: 20261106_0014
Revises: 20261104_0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20261106_0014"
down_revision = "20261104_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("run_state", sa.String(length=32), nullable=True))
    op.create_index("ix_courses_run_state", "courses", ["run_state"])


def downgrade() -> None:
    op.drop_index("ix_courses_run_state", table_name="courses")
    op.drop_column("courses", "run_state")
