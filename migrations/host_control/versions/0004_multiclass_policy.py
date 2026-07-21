"""Normalize signed multi-class endpoint policy and atomic Reserve v2."""

from alembic import op

revision = "0004_multiclass_policy"
down_revision = "0003_fencing_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE rate_control.authority_blocks (
            endpoint_authority varchar(96) PRIMARY KEY,
            blocked_until timestamptz,
            reason_code varchar(64) NOT NULL,
            consecutive_backoff_count integer NOT NULL DEFAULT 0
              CHECK (consecutive_backoff_count >= 0),
            source_message_id varchar(128) NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        ALTER TABLE rate_control.permits
          ADD COLUMN gateway_connection_id varchar(128)
        """
    )
    op.execute(
        """
        ALTER TABLE rate_control.endpoint_runtime_policies
          ADD COLUMN allowed_operation_classes varchar(48)[],
          ADD COLUMN causal_role_class_map jsonb,
          ADD COLUMN class_cost_vectors jsonb,
          ADD COLUMN endpoint_contract_payload jsonb,
          ADD COLUMN endpoint_contract_hash char(64)
            CHECK (endpoint_contract_hash IS NULL OR endpoint_contract_hash ~ '^[0-9a-f]{64}$')
        """
    )
    op.execute(
        """
        ALTER TABLE rate_control.endpoint_runtime_policies
          DISABLE TRIGGER endpoint_runtime_policies_append_only
        """
    )
    op.execute(
        """
        UPDATE rate_control.endpoint_runtime_policies
           SET allowed_operation_classes = ARRAY[derived_operation_class],
               causal_role_class_map = jsonb_build_object(
                 derived_operation_class, derived_operation_class
               ),
               class_cost_vectors = jsonb_build_object(derived_operation_class, cost_vector)
        """
    )
    op.execute(
        """
        ALTER TABLE rate_control.endpoint_runtime_policies
          ENABLE TRIGGER endpoint_runtime_policies_append_only
        """
    )
    op.execute(
        """
        ALTER TABLE rate_control.endpoint_runtime_policies
          ALTER COLUMN allowed_operation_classes SET NOT NULL,
          ALTER COLUMN causal_role_class_map SET NOT NULL,
          ALTER COLUMN class_cost_vectors SET NOT NULL,
          ADD CONSTRAINT endpoint_policy_classes_nonempty
            CHECK (cardinality(allowed_operation_classes) > 0),
          ADD CONSTRAINT endpoint_policy_causal_map_object
            CHECK (jsonb_typeof(causal_role_class_map) = 'object'),
          ADD CONSTRAINT endpoint_policy_class_costs_object
            CHECK (jsonb_typeof(class_cost_vectors) = 'object')
        """
    )
    op.execute(
        """
        CREATE FUNCTION rate_control.reserve_permit_v2_under_active_lease(
            requested_permit_id varchar,
            requested_request_key varchar,
            requested_subject_service varchar,
            requested_subject_instance varchar,
            requested_environment varchar,
            requested_gateway_connection_id varchar,
            requested_endpoint_authority varchar,
            requested_endpoint_id varchar,
            requested_catalog_hash char(64),
            requested_derived_operation_class varchar,
            requested_canonical_hash char(64),
            requested_parameter_hash char(64),
            requested_wire_hash char(64),
            requested_operation_facts_hash char(64),
            requested_capability_hash char(64),
            requested_document_hash char(64),
            requested_capability_nonce varchar,
            requested_fencing_epoch bigint,
            requested_expires_at timestamptz
        ) RETURNS TABLE(
            decision varchar,
            reason_code varchar,
            permit_id varchar,
            derived_operation_class varchar,
            fencing_epoch bigint,
            expires_at timestamptz
        )
        LANGUAGE plpgsql AS $$
        DECLARE
            now_at timestamptz := clock_timestamp();
            current_epoch bigint;
            policy_row rate_control.endpoint_runtime_policies%ROWTYPE;
            existing_permit rate_control.permits%ROWTYPE;
            window_row rate_control.rate_windows%ROWTYPE;
            selected_cost_vector jsonb;
            cost_item jsonb;
            item_cost bigint;
            item_ceiling bigint;
            used_before bigint;
            used_after bigint;
        BEGIN
            SELECT epoch INTO current_epoch
              FROM rate_control.fencing_state WHERE singleton = true FOR UPDATE;
            IF current_epoch IS NULL OR current_epoch <> requested_fencing_epoch THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_FENCING_STALE'::varchar,
                  NULL::varchar, NULL::varchar, COALESCE(current_epoch, 0), NULL::timestamptz;
                RETURN;
            END IF;
            IF requested_expires_at <= now_at
               OR requested_expires_at > now_at + interval '5 seconds' THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_AUTHORITY_UNAVAILABLE'::varchar,
                  NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                RETURN;
            END IF;
            IF EXISTS (
              SELECT 1 FROM rate_control.authority_blocks
               WHERE endpoint_authority = requested_endpoint_authority
                 AND (blocked_until IS NULL OR blocked_until > now_at)
            ) THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_SCOPE_BLOCKED'::varchar,
                  NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                RETURN;
            END IF;

            SELECT * INTO existing_permit FROM rate_control.permits
              WHERE request_key = requested_request_key FOR UPDATE;
            IF FOUND THEN
                IF existing_permit.state = 'RESERVED'
                   AND existing_permit.expires_at > now_at
                   AND existing_permit.subject_caller_service = requested_subject_service
                   AND existing_permit.subject_caller_instance_id = requested_subject_instance
                   AND existing_permit.environment = requested_environment
                   AND existing_permit.gateway_connection_id IS NOT DISTINCT FROM
                       requested_gateway_connection_id
                   AND existing_permit.endpoint_authority = requested_endpoint_authority
                   AND existing_permit.endpoint_id = requested_endpoint_id
                   AND existing_permit.endpoint_catalog_hash = requested_catalog_hash
                   AND existing_permit.derived_operation_class = requested_derived_operation_class
                   AND existing_permit.canonical_request_hash = requested_canonical_hash
                   AND existing_permit.parameter_hash = requested_parameter_hash
                   AND existing_permit.wire_bytes_hash = requested_wire_hash
                   AND existing_permit.operation_facts_hash = requested_operation_facts_hash
                   AND existing_permit.capability_payload_hash = requested_capability_hash
                   AND existing_permit.gateway_request_document_hash = requested_document_hash
                   AND existing_permit.capability_nonce = requested_capability_nonce
                   AND existing_permit.fencing_epoch = requested_fencing_epoch THEN
                    RETURN QUERY SELECT 'GRANTED'::varchar, 'RATE_GRANTED'::varchar,
                      existing_permit.permit_id, existing_permit.derived_operation_class,
                      existing_permit.fencing_epoch, existing_permit.expires_at;
                ELSE
                    RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_CAPABILITY_REPLAYED'::varchar,
                      NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                END IF;
                RETURN;
            END IF;

            SELECT * INTO policy_row FROM rate_control.endpoint_runtime_policies AS policy
             WHERE policy.endpoint_authority = requested_endpoint_authority
               AND policy.endpoint_id = requested_endpoint_id
               AND policy.endpoint_catalog_hash = requested_catalog_hash;
            IF NOT FOUND OR policy_row.status <> 'SIGNED_RUNTIME'
               OR now_at < policy_row.valid_from OR now_at >= policy_row.valid_until
               OR policy_row.endpoint_contract_payload IS NULL
               OR policy_row.endpoint_contract_hash IS NULL THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_ENDPOINT_UNKNOWN'::varchar,
                  NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                RETURN;
            END IF;
            IF NOT requested_subject_service = ANY(policy_row.allowed_callers) THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_CALLER_NOT_ALLOWED'::varchar,
                  NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                RETURN;
            END IF;
            IF NOT requested_derived_operation_class = ANY(policy_row.allowed_operation_classes)
               OR NOT policy_row.class_cost_vectors ? requested_derived_operation_class THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_CAUSAL_ROLE_INVALID'::varchar,
                  NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                RETURN;
            END IF;
            selected_cost_vector :=
              policy_row.class_cost_vectors -> requested_derived_operation_class;
            IF jsonb_typeof(selected_cost_vector) <> 'array'
               OR jsonb_array_length(selected_cost_vector) < 1 THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_AUTHORITY_UNAVAILABLE'::varchar,
                  NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                RETURN;
            END IF;

            FOR cost_item IN
                SELECT value FROM jsonb_array_elements(selected_cost_vector)
                 ORDER BY value->>'rate_limit_type', value->>'scope_key_hash',
                          value->>'interval_name'
            LOOP
                item_cost := (cost_item->>'cost')::bigint;
                item_ceiling := (cost_item->>'ceiling_units')::bigint;
                IF item_cost < 0 OR item_ceiling < 1 THEN
                    RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_AUTHORITY_UNAVAILABLE'::varchar,
                      NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                    RETURN;
                END IF;
                SELECT * INTO window_row FROM rate_control.rate_windows
                 WHERE endpoint_authority = requested_endpoint_authority
                   AND scope_key_hash = cost_item->>'scope_key_hash'
                   AND rate_limit_type = cost_item->>'rate_limit_type'
                   AND interval_name = cost_item->>'interval_name'
                   AND window_start <= now_at AND window_end > now_at
                 FOR UPDATE;
                IF NOT FOUND THEN
                    RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_AUTHORITY_UNAVAILABLE'::varchar,
                      NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                    RETURN;
                END IF;
                IF window_row.blocked_until IS NOT NULL AND window_row.blocked_until > now_at THEN
                    RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_SCOPE_BLOCKED'::varchar,
                      NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                    RETURN;
                END IF;
                used_before := GREATEST(window_row.effective_used, window_row.observed_max);
                IF used_before + item_cost > LEAST(window_row.hard_limit, item_ceiling) THEN
                    RETURN QUERY SELECT 'DENIED'::varchar,
                      'RATE_CLASS_CEILING_EXCEEDED'::varchar, NULL::varchar, NULL::varchar,
                      current_epoch, NULL::timestamptz;
                    RETURN;
                END IF;
            END LOOP;

            INSERT INTO rate_control.capability_nonces(nonce,payload_hash,state,expires_at)
              VALUES(requested_capability_nonce, requested_capability_hash, 'RESERVED',
                     requested_expires_at)
              ON CONFLICT DO NOTHING;
            IF NOT FOUND THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_CAPABILITY_REPLAYED'::varchar,
                  NULL::varchar, NULL::varchar, current_epoch, NULL::timestamptz;
                RETURN;
            END IF;

            INSERT INTO rate_control.permits(
              permit_id,request_key,subject_caller_service,subject_caller_instance_id,
              environment,gateway_connection_id,endpoint_authority,endpoint_id,
              endpoint_catalog_hash,
              derived_operation_class,canonical_request_hash,parameter_hash,wire_bytes_hash,
              operation_facts_hash,capability_payload_hash,gateway_request_document_hash,
              capability_nonce,fencing_epoch,state,reserved_at,expires_at
            ) VALUES (
              requested_permit_id,requested_request_key,requested_subject_service,
              requested_subject_instance,requested_environment,requested_gateway_connection_id,
              requested_endpoint_authority,requested_endpoint_id,requested_catalog_hash,
              requested_derived_operation_class,
              requested_canonical_hash,requested_parameter_hash,requested_wire_hash,
              requested_operation_facts_hash,requested_capability_hash,requested_document_hash,
              requested_capability_nonce,requested_fencing_epoch,'RESERVED',now_at,
              requested_expires_at
            );

            FOR cost_item IN
                SELECT value FROM jsonb_array_elements(selected_cost_vector)
                 ORDER BY value->>'rate_limit_type', value->>'scope_key_hash',
                          value->>'interval_name'
            LOOP
                item_cost := (cost_item->>'cost')::bigint;
                SELECT * INTO window_row FROM rate_control.rate_windows
                 WHERE endpoint_authority = requested_endpoint_authority
                   AND scope_key_hash = cost_item->>'scope_key_hash'
                   AND rate_limit_type = cost_item->>'rate_limit_type'
                   AND interval_name = cost_item->>'interval_name'
                   AND window_start <= now_at AND window_end > now_at
                 FOR UPDATE;
                used_before := GREATEST(window_row.effective_used, window_row.observed_max);
                used_after := used_before + item_cost;
                UPDATE rate_control.rate_windows
                   SET effective_used = used_after, updated_at = now_at
                 WHERE window_id = window_row.window_id;
                INSERT INTO rate_control.allocations(
                  permit_id,window_id,cost,effective_used_before,effective_used_after
                ) VALUES (
                  requested_permit_id,window_row.window_id,item_cost,used_before,used_after
                );
            END LOOP;
            RETURN QUERY SELECT 'GRANTED'::varchar, 'RATE_GRANTED'::varchar,
              requested_permit_id, requested_derived_operation_class, current_epoch,
              requested_expires_at;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION rate_control.reserve_permit_v2(
            requested_permit_id varchar,
            requested_request_key varchar,
            requested_subject_service varchar,
            requested_subject_instance varchar,
            requested_environment varchar,
            requested_gateway_connection_id varchar,
            requested_endpoint_authority varchar,
            requested_endpoint_id varchar,
            requested_catalog_hash char(64),
            requested_derived_operation_class varchar,
            requested_canonical_hash char(64),
            requested_parameter_hash char(64),
            requested_wire_hash char(64),
            requested_operation_facts_hash char(64),
            requested_capability_hash char(64),
            requested_document_hash char(64),
            requested_capability_nonce varchar,
            requested_fencing_epoch bigint,
            requested_expires_at timestamptz
        ) RETURNS TABLE(
            decision varchar,
            reason_code varchar,
            permit_id varchar,
            derived_operation_class varchar,
            fencing_epoch bigint,
            expires_at timestamptz
        )
        LANGUAGE plpgsql AS $$
        DECLARE
            state_row rate_control.fencing_state%ROWTYPE;
        BEGIN
            SELECT * INTO state_row FROM rate_control.fencing_state
              WHERE singleton = true FOR UPDATE;
            IF NOT FOUND OR state_row.epoch <> requested_fencing_epoch
               OR state_row.lease_expires_at IS NULL
               OR state_row.lease_expires_at <= clock_timestamp() THEN
                RETURN QUERY SELECT 'DENIED'::varchar, 'RATE_FENCING_STALE'::varchar,
                  NULL::varchar, NULL::varchar, COALESCE(state_row.epoch, 0), NULL::timestamptz;
                RETURN;
            END IF;
            RETURN QUERY SELECT * FROM rate_control.reserve_permit_v2_under_active_lease(
              requested_permit_id,requested_request_key,requested_subject_service,
              requested_subject_instance,requested_environment,requested_gateway_connection_id,
              requested_endpoint_authority,requested_endpoint_id,requested_catalog_hash,
              requested_derived_operation_class,
              requested_canonical_hash,requested_parameter_hash,requested_wire_hash,
              requested_operation_facts_hash,requested_capability_hash,requested_document_hash,
              requested_capability_nonce,requested_fencing_epoch,requested_expires_at
            );
        END $$
        """
    )


def downgrade() -> None:
    signature = (
        "varchar,varchar,varchar,varchar,varchar,varchar,varchar,varchar,char,varchar,"
        "char,char,char,char,char,char,varchar,bigint,timestamptz"
    )
    op.execute(f"DROP FUNCTION rate_control.reserve_permit_v2({signature})")
    op.execute(f"DROP FUNCTION rate_control.reserve_permit_v2_under_active_lease({signature})")
    op.execute("ALTER TABLE rate_control.permits DROP COLUMN gateway_connection_id")
    op.execute(
        "ALTER TABLE rate_control.endpoint_runtime_policies "
        "DROP CONSTRAINT endpoint_policy_class_costs_object, "
        "DROP CONSTRAINT endpoint_policy_causal_map_object, "
        "DROP CONSTRAINT endpoint_policy_classes_nonempty, "
        "DROP COLUMN endpoint_contract_hash, DROP COLUMN endpoint_contract_payload, "
        "DROP COLUMN class_cost_vectors, DROP COLUMN causal_role_class_map, "
        "DROP COLUMN allowed_operation_classes"
    )
    op.execute("DROP TABLE rate_control.authority_blocks")
