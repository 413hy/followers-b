"""Bind permit consumption to the complete gateway-derived request envelope."""

from alembic import op

revision = "0006_consume_v2"
down_revision = "0005_gateway_journal"
branch_labels = None
depends_on = None

CONSUME_V2_ARGUMENTS = (
    "varchar,varchar,varchar,varchar,varchar,varchar,varchar,char,char,char,char,char,"
    "char,char,bigint,varchar"
)


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION rate_control.consume_permit_v2(
            requested_permit_id varchar,
            requested_subject_service varchar,
            requested_subject_instance varchar,
            requested_environment varchar,
            requested_endpoint_authority varchar,
            requested_endpoint_id varchar,
            requested_gateway_connection_id varchar,
            requested_catalog_hash char(64),
            requested_canonical_hash char(64),
            requested_parameter_hash char(64),
            requested_wire_hash char(64),
            requested_operation_facts_hash char(64),
            requested_capability_hash char(64),
            requested_document_hash char(64),
            requested_fencing_epoch bigint,
            consuming_gateway_instance_id varchar
        ) RETURNS TABLE(decision varchar, reason_code varchar, send_deadline timestamptz)
        LANGUAGE plpgsql AS $$
        DECLARE
            permit_row rate_control.permits%ROWTYPE;
            state_row rate_control.fencing_state%ROWTYPE;
            now_at timestamptz := clock_timestamp();
        BEGIN
            SELECT * INTO state_row FROM rate_control.fencing_state
             WHERE singleton = true FOR UPDATE;
            IF NOT FOUND OR state_row.epoch <> requested_fencing_epoch
               OR state_row.lease_expires_at IS NULL
               OR state_row.lease_expires_at <= now_at THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_FENCING_STALE'::varchar, NULL::timestamptz;
                RETURN;
            END IF;

            SELECT * INTO permit_row FROM rate_control.permits
             WHERE permit_id = requested_permit_id FOR UPDATE;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_PERMIT_UNKNOWN'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.state = 'CONSUMED' THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_PERMIT_ALREADY_CONSUMED'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.state = 'EXPIRED' OR now_at >= permit_row.expires_at THEN
                UPDATE rate_control.permits SET state = 'EXPIRED'
                 WHERE permit_id = requested_permit_id AND state = 'RESERVED';
                UPDATE rate_control.capability_nonces SET state = 'EXPIRED'
                 WHERE nonce = permit_row.capability_nonce AND state = 'RESERVED';
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_PERMIT_EXPIRED'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.subject_caller_service <> requested_subject_service
               OR permit_row.subject_caller_instance_id <> requested_subject_instance
               OR permit_row.environment <> requested_environment
               OR permit_row.endpoint_authority <> requested_endpoint_authority
               OR permit_row.endpoint_id <> requested_endpoint_id
               OR permit_row.gateway_connection_id IS DISTINCT FROM
                  requested_gateway_connection_id
               OR permit_row.endpoint_catalog_hash <> requested_catalog_hash
               OR permit_row.canonical_request_hash <> requested_canonical_hash THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_PERMIT_BINDING_MISMATCH'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.parameter_hash <> requested_parameter_hash THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_PARAMETER_HASH_MISMATCH'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.wire_bytes_hash <> requested_wire_hash THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_WIRE_HASH_MISMATCH'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.gateway_request_document_hash <> requested_document_hash THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_REQUEST_DOCUMENT_HASH_MISMATCH'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.operation_facts_hash <> requested_operation_facts_hash THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_OPERATION_FACTS_MISMATCH'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            IF permit_row.capability_payload_hash <> requested_capability_hash THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_CAPABILITY_INVALID_OR_REPLAYED'::varchar, NULL::timestamptz;
                RETURN;
            END IF;

            UPDATE rate_control.capability_nonces
               SET state = 'CONSUMED', consumed_at = now_at
             WHERE nonce = permit_row.capability_nonce
               AND payload_hash = requested_capability_hash
               AND state = 'RESERVED' AND expires_at > now_at;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'CONSUME_DENIED'::varchar,
                  'RATE_CAPABILITY_INVALID_OR_REPLAYED'::varchar, NULL::timestamptz;
                RETURN;
            END IF;
            UPDATE rate_control.permits
               SET state = 'CONSUMED', consumed_at = now_at,
                   gateway_instance_id = consuming_gateway_instance_id
             WHERE permit_id = requested_permit_id;
            RETURN QUERY SELECT 'CONSUME_GRANTED'::varchar,
              'RATE_PERMIT_CONSUMED'::varchar,
              LEAST(now_at + interval '50 milliseconds', permit_row.expires_at);
        END $$
        """
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION rate_control.consume_permit_v2({CONSUME_V2_ARGUMENTS})")
