"""EAOC Human-in-the-Loop (HITL) Approval Requests Persistence Table

Revision ID: 0002_approval_requests
Revises: 0001_initial_foundation
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0002_approval_requests'
down_revision: Union[str, None] = '0001_initial_foundation'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'approval_requests',
        sa.Column('approval_id', sa.String(length=50), nullable=False),
        sa.Column('workflow_id', sa.String(length=50), nullable=False),
        sa.Column('tenant_id', sa.String(length=50), nullable=False),
        sa.Column('task_id', sa.String(length=50), nullable=True),
        sa.Column('step_index', sa.Integer(), nullable=True),
        sa.Column('requested_action', sa.Text(), nullable=False),
        sa.Column('risk_level', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('decision', sa.String(length=20), nullable=True),
        sa.Column('approver_id', sa.String(length=100), nullable=True),
        sa.Column('decision_reason', sa.Text(), nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('resume_status', sa.String(length=20), server_default='none', nullable=False),
        sa.Column('resume_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED')", name='chk_approval_requests_status'),
        sa.CheckConstraint("risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')", name='chk_approval_requests_risk_level'),
        sa.CheckConstraint("resume_status IN ('none', 'pending', 'resumed', 'failed')", name='chk_approval_requests_resume_status'),
        sa.PrimaryKeyConstraint('approval_id', name='pk_approval_requests'),
    )

    op.create_index('ix_approval_requests_workflow_id', 'approval_requests', ['workflow_id'], unique=False)
    op.create_index('ix_approval_requests_tenant_id', 'approval_requests', ['tenant_id'], unique=False)
    op.create_index('ix_approval_requests_expires_at', 'approval_requests', ['expires_at'], unique=False)
    op.create_index(
        'ix_approval_tenant_status_expires',
        'approval_requests',
        ['tenant_id', 'status', 'expires_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_approval_tenant_status_expires', table_name='approval_requests')
    op.drop_index('ix_approval_requests_expires_at', table_name='approval_requests')
    op.drop_index('ix_approval_requests_tenant_id', table_name='approval_requests')
    op.drop_index('ix_approval_requests_workflow_id', table_name='approval_requests')
    op.drop_table('approval_requests')
