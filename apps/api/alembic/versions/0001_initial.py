"""initial schema

Bootstrap migration creates the full schema from current model metadata.
All subsequent migrations must use `alembic revision --autogenerate`.
"""

from alembic import op

from app.db.base import Base
from app.db import models  # noqa: F401

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
