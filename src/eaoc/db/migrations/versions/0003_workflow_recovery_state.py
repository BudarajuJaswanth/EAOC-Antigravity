"""EAOC Workflow State & Step Provenance Recovery Persistence Tables

Revision ID: 0003_workflow_recovery_state
Revises: 0002_approval_requests
Create Date: 2026-10-24 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0003_workflow_recovery_state'
down_revision: Union[str, None] = '0002_approval_requests'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Authoritative Workflows Recovery Table
    op.create_table(
        'workflows',
        sa.Column('workflow_id', sa.String(length=50), nullable=False),
        sa.Column('tenant_id', sa.String(length=50), nullable=False),
        sa.Column('user_id', sa.String(length=100), nullable=False),
        sa.Column('task_id', sa.String(length=50), nullable=False),
        sa.Column('task_description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
        sa.Column('current_step_index', sa.Integer(), nullable=True),
        sa.Column('execution_plan', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('plan_hash', sa.String(length=64), nullable=True),
        sa.Column('fencing_epoch', sa.Integer(), server_default='0', nullable=False),
        sa.Column('row_revision', sa.Integer(), server_default='1', nullable=False),
        sa.Column('lock_token', sa.String(length=64), nullable=True),
        sa.Column('lock_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('idempotency_key', sa.String(length=100), nullable=True),
        sa.Column('idempotency_fingerprint', sa.String(length=64), nullable=True),
        sa.Column('execution_metadata', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'WAITING_APPROVAL', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name='chk_workflows_status'
        ),
        sa.PrimaryKeyConstraint('workflow_id', name='pk_workflows'),
    )
    op.create_index('ix_workflows_tenant_id', 'workflows', ['tenant_id'], unique=False)
    op.create_index('ix_workflows_user_id', 'workflows', ['user_id'], unique=False)
    op.create_index('ix_workflows_tenant_status', 'workflows', ['tenant_id', 'status'], unique=False)
    op.create_index(
        'ix_workflows_tenant_idempotency',
        'workflows',
        ['tenant_id', 'idempotency_key'],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # 2. Authoritative Step Execution Ledger & Authentic Provenance Table
    op.create_table(
        'workflow_tasks',
        sa.Column('task_id', sa.String(length=100), nullable=False),
        sa.Column('workflow_id', sa.String(length=50), nullable=False),
        sa.Column('tenant_id', sa.String(length=50), nullable=False),
        sa.Column('step_index', sa.Integer(), nullable=False),
        sa.Column('agent_id', sa.String(length=50), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('plan_hash', sa.String(length=64), nullable=False),
        sa.Column('step_hash', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='PENDING', nullable=False),
        sa.Column('input_data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('output_data', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('provenance', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
        sa.Column('duration_ms', sa.Float(), nullable=True),
        sa.Column('error', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'BLOCKED')",
            name='chk_workflow_tasks_status'
        ),
        sa.ForeignKeyConstraint(['workflow_id'], ['workflows.workflow_id'], name='fk_workflow_tasks_workflow_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('task_id', name='pk_workflow_tasks'),
        sa.UniqueConstraint('workflow_id', 'step_index', name='uq_workflow_step_index'),
    )
    op.create_index('ix_workflow_tasks_workflow_id', 'workflow_tasks', ['workflow_id'], unique=False)
    op.create_index('ix_workflow_tasks_tenant_id', 'workflow_tasks', ['tenant_id'], unique=False)
    op.create_index('ix_workflow_tasks_lookup', 'workflow_tasks', ['tenant_id', 'workflow_id', 'step_index'], unique=False)

    # 3. Add Foreign Key on existing approval_requests table with ON DELETE RESTRICT NOT VALID
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'fk_approval_requests_workflow_id'
            ) THEN
                ALTER TABLE approval_requests
                ADD CONSTRAINT fk_approval_requests_workflow_id
                FOREIGN KEY (workflow_id) REFERENCES workflows(workflow_id)
                ON DELETE RESTRICT
                NOT VALID;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # 1. Drop foreign key on approval_requests
    op.execute(
        """
        ALTER TABLE approval_requests
        DROP CONSTRAINT IF EXISTS fk_approval_requests_workflow_id;
        """
    )

    # 2. Drop workflow_tasks table
    op.drop_index('ix_workflow_tasks_lookup', table_name='workflow_tasks')
    op.drop_index('ix_workflow_tasks_tenant_id', table_name='workflow_tasks')
    op.drop_index('ix_workflow_tasks_workflow_id', table_name='workflow_tasks')
    op.drop_table('workflow_tasks')

    # 3. Drop workflows table
    op.drop_index('ix_workflows_tenant_idempotency', table_name='workflows')
    op.drop_index('ix_workflows_tenant_status', table_name='workflows')
    op.drop_index('ix_workflows_user_id', table_name='workflows')
    op.drop_index('ix_workflows_tenant_id', table_name='workflows')
    op.drop_table('workflows')
