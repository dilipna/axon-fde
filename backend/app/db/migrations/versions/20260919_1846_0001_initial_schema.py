"""Initial application schema: all 12 tables.

Generated from ``models.Base.metadata`` and then reviewed, which is the only
way autogenerate should ever be used. Two things it could not know about were
added by hand:

- ``CREATE EXTENSION vector``. The pgvector image ships the extension but does
  not enable it, and Phase 4's retrieval work needs it present from the start
  rather than bolted on by a later migration that has to backfill.
- Nothing here grants anything. Privileges are migration 0002's job, because
  the append-only story is a unit and splitting it across two files would let
  half of it be applied.

Revision ID: 0001
Revises: 
Created: 2026-09-19 18:46:58.017207+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Enabled here rather than in a later migration so that no deployment ever
    # exists with the schema but without the extension the retrieval path
    # assumes. Requires the owner role, which is what runs migrations.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table('artifact',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('storage_uri', sa.String(length=512), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('content_type', sa.String(length=128), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('uploaded_by', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sha256')
    )
    op.create_table('incident',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('correlation_key', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('severity', sa.String(length=16), nullable=False),
    sa.Column('incident_type', sa.String(length=64), nullable=False),
    sa.Column('entity_kind', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.String(length=64), nullable=False),
    sa.Column('detected_by', sa.String(length=16), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('predicted_breach_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('superseded_by', sa.Uuid(), nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('scenario_run_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['superseded_by'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_incident_correlation_active', 'incident', ['correlation_key', 'status'], unique=False)
    op.create_index('ix_incident_entity', 'incident', ['entity_kind', 'entity_id'], unique=False)
    op.create_index('ix_incident_status', 'incident', ['status'], unique=False)
    op.create_table('action_candidate',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=False),
    sa.Column('action_type', sa.String(length=48), nullable=False),
    sa.Column('target_ref', sa.String(length=128), nullable=True),
    sa.Column('feasible', sa.Boolean(), nullable=False),
    sa.Column('infeasibility_reason', sa.Text(), nullable=True),
    sa.Column('est_cost_usd', sa.Float(), nullable=True),
    sa.Column('est_delay_min', sa.Float(), nullable=True),
    sa.Column('predicted_risk_after', sa.Float(), nullable=True),
    sa.Column('predicted_risk_ci', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ev_total', sa.Float(), nullable=True),
    sa.Column('ev_breakdown', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('assumptions', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('required_approval_role', sa.String(length=32), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_candidate_incident', 'action_candidate', ['incident_id'], unique=False)
    op.create_table('audit_event',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('event_hash', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.String(length=128), nullable=False),
    sa.Column('actor_role', sa.String(length=32), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('subject_kind', sa.String(length=32), nullable=False),
    sa.Column('subject_id', sa.String(length=64), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('event_hash'),
    sa.UniqueConstraint('seq')
    )
    op.create_index('ix_audit_incident', 'audit_event', ['incident_id'], unique=False)
    op.create_index('ix_audit_seq', 'audit_event', ['seq'], unique=False)
    op.create_index('ix_audit_subject', 'audit_event', ['subject_kind', 'subject_id'], unique=False)
    op.create_table('evidence',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=True),
    sa.Column('entity_kind', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('modality', sa.String(length=16), nullable=False),
    sa.Column('observation_type', sa.String(length=64), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('unit', sa.String(length=32), nullable=True),
    sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('confidence', sa.Float(), nullable=False),
    sa.Column('confidence_basis', sa.String(length=32), nullable=False),
    sa.Column('freshness', sa.String(length=16), nullable=False),
    sa.Column('provenance', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('artifact_id', sa.Uuid(), nullable=True),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.CheckConstraint("source IN ('sql_legacy','telemetry','document_extraction','visual_inspection','weather_api','risk_model','sop_retrieval','human_input')", name='ck_evidence_source_never_llm'),
    sa.CheckConstraint('confidence >= 0 AND confidence <= 1', name='ck_evidence_confidence'),
    sa.ForeignKeyConstraint(['artifact_id'], ['artifact.id'], ),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_evidence_incident', 'evidence', ['incident_id'], unique=False)
    op.create_index('ix_evidence_reconcile', 'evidence', ['observation_type', 'entity_kind', 'entity_id', 'status'], unique=False)
    op.create_table('model_invocation',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=True),
    sa.Column('node', sa.String(length=64), nullable=False),
    sa.Column('model_id', sa.String(length=64), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_estimate_usd', sa.Float(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_invocation_incident', 'model_invocation', ['incident_id'], unique=False)
    op.create_table('risk_assessment',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=False),
    sa.Column('horizon_minutes', sa.Integer(), nullable=False),
    sa.Column('probability', sa.Float(), nullable=False),
    sa.Column('baseline_probability', sa.Float(), nullable=False),
    sa.Column('baseline_name', sa.String(length=64), nullable=False),
    sa.Column('model_version', sa.String(length=64), nullable=False),
    sa.Column('calibration_version', sa.String(length=64), nullable=True),
    sa.Column('feature_vector_hash', sa.String(length=64), nullable=False),
    sa.Column('degraded', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('baseline_probability >= 0 AND baseline_probability <= 1', name='ck_risk_baseline'),
    sa.CheckConstraint('probability >= 0 AND probability <= 1', name='ck_risk_probability'),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_risk_incident', 'risk_assessment', ['incident_id'], unique=False)
    op.create_table('evidence_link',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('from_evidence_id', sa.Uuid(), nullable=False),
    sa.Column('to_evidence_id', sa.Uuid(), nullable=False),
    sa.Column('relation', sa.String(length=16), nullable=False),
    sa.Column('detected_by', sa.String(length=16), nullable=False),
    sa.Column('rule_id', sa.String(length=64), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('from_evidence_id <> to_evidence_id', name='ck_link_not_self'),
    sa.ForeignKeyConstraint(['from_evidence_id'], ['evidence.id'], ),
    sa.ForeignKeyConstraint(['to_evidence_id'], ['evidence.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('from_evidence_id', 'to_evidence_id', 'relation', name='uq_evidence_link')
    )
    op.create_index('ix_link_relation', 'evidence_link', ['relation'], unique=False)
    op.create_table('recommendation',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=False),
    sa.Column('selected_action_id', sa.Uuid(), nullable=False),
    sa.Column('narrative', sa.Text(), nullable=False),
    sa.Column('cited_evidence_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('flip_sensitivity', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('grounding_check', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('prompt_version', sa.String(length=32), nullable=True),
    sa.Column('model_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.ForeignKeyConstraint(['selected_action_id'], ['action_candidate.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_recommendation_incident', 'recommendation', ['incident_id'], unique=False)
    op.create_table('approval',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('recommendation_id', sa.Uuid(), nullable=False),
    sa.Column('action_candidate_id', sa.Uuid(), nullable=False),
    sa.Column('bound_context_hash', sa.String(length=64), nullable=False),
    sa.Column('required_role', sa.String(length=32), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=True),
    sa.Column('decided_by', sa.String(length=128), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('expires_at > requested_at', name='ck_approval_expiry'),
    sa.ForeignKeyConstraint(['action_candidate_id'], ['action_candidate.id'], ),
    sa.ForeignKeyConstraint(['recommendation_id'], ['recommendation.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_approval_pending', 'approval', ['decision', 'expires_at'], unique=False)
    op.create_table('action_execution',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('approval_id', sa.Uuid(), nullable=True),
    sa.Column('incident_id', sa.Uuid(), nullable=False),
    sa.Column('idempotency_key', sa.String(length=128), nullable=False),
    sa.Column('action_type', sa.String(length=48), nullable=False),
    sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['approval_id'], ['approval.id'], ),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    op.create_index('ix_execution_incident', 'action_execution', ['incident_id'], unique=False)
    op.create_table('outcome_verification',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('incident_id', sa.Uuid(), nullable=False),
    sa.Column('execution_id', sa.Uuid(), nullable=True),
    sa.Column('expected_effect', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('observed_effect', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('window_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('window_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('verdict', sa.String(length=16), nullable=True),
    sa.Column('follow_up_required', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['execution_id'], ['action_execution.id'], ),
    sa.ForeignKeyConstraint(['incident_id'], ['incident.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_verification_incident', 'outcome_verification', ['incident_id'], unique=False)



def downgrade() -> None:
    # The extension is deliberately not dropped: another database in the same
    # cluster may be using it, and dropping it would take their columns with
    # it via CASCADE.
    op.drop_index('ix_verification_incident', table_name='outcome_verification')
    op.drop_table('outcome_verification')
    op.drop_index('ix_execution_incident', table_name='action_execution')
    op.drop_table('action_execution')
    op.drop_index('ix_approval_pending', table_name='approval')
    op.drop_table('approval')
    op.drop_index('ix_recommendation_incident', table_name='recommendation')
    op.drop_table('recommendation')
    op.drop_index('ix_link_relation', table_name='evidence_link')
    op.drop_table('evidence_link')
    op.drop_index('ix_risk_incident', table_name='risk_assessment')
    op.drop_table('risk_assessment')
    op.drop_index('ix_invocation_incident', table_name='model_invocation')
    op.drop_table('model_invocation')
    op.drop_index('ix_evidence_reconcile', table_name='evidence')
    op.drop_index('ix_evidence_incident', table_name='evidence')
    op.drop_table('evidence')
    op.drop_index('ix_audit_subject', table_name='audit_event')
    op.drop_index('ix_audit_seq', table_name='audit_event')
    op.drop_index('ix_audit_incident', table_name='audit_event')
    op.drop_table('audit_event')
    op.drop_index('ix_candidate_incident', table_name='action_candidate')
    op.drop_table('action_candidate')
    op.drop_index('ix_incident_status', table_name='incident')
    op.drop_index('ix_incident_entity', table_name='incident')
    op.drop_index('ix_incident_correlation_active', table_name='incident')
    op.drop_table('incident')
    op.drop_table('artifact')
