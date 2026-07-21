"""Reconcile verified response headers and persist 429/418 authority blocks."""

from alembic import op

revision = "0007_header_reconciliation"
down_revision = "0006_consume_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION rate_control.reconcile_header_observation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            header_item record;
            suffix varchar;
            interval_name_value varchar;
            observed_value bigint;
            status_code integer;
            retry_seconds integer;
            computed_block_until timestamptz;
            current_count integer;
        BEGIN
            IF NEW.observation_type <> 'HeaderObservation' THEN
                RETURN NEW;
            END IF;
            FOR header_item IN
                SELECT key, value FROM jsonb_each_text(
                  COALESCE(NEW.payload->'used_weight_observations', '{}'::jsonb)
                )
            LOOP
                suffix := substring(header_item.key from '([0-9]+[smhd])$');
                IF suffix IS NULL THEN CONTINUE; END IF;
                interval_name_value := CASE right(suffix, 1)
                  WHEN 's' THEN 'SECOND_' WHEN 'm' THEN 'MINUTE_'
                  WHEN 'h' THEN 'HOUR_' WHEN 'd' THEN 'DAY_' END
                  || left(suffix, length(suffix) - 1);
                observed_value := header_item.value::bigint;
                UPDATE rate_control.rate_windows AS rate_window
                   SET observed_max = GREATEST(rate_window.observed_max, observed_value),
                       updated_at = clock_timestamp()
                  FROM rate_control.allocations AS allocation
                 WHERE allocation.window_id = rate_window.window_id
                   AND allocation.permit_id = NEW.permit_id
                   AND rate_window.rate_limit_type = 'REQUEST_WEIGHT'
                   AND rate_window.interval_name = interval_name_value;
            END LOOP;
            FOR header_item IN
                SELECT key, value FROM jsonb_each_text(
                  COALESCE(NEW.payload->'order_count_observations', '{}'::jsonb)
                )
            LOOP
                suffix := substring(header_item.key from '([0-9]+[smhd])$');
                IF suffix IS NULL THEN CONTINUE; END IF;
                interval_name_value := CASE right(suffix, 1)
                  WHEN 's' THEN 'SECOND_' WHEN 'm' THEN 'MINUTE_'
                  WHEN 'h' THEN 'HOUR_' WHEN 'd' THEN 'DAY_' END
                  || left(suffix, length(suffix) - 1);
                observed_value := header_item.value::bigint;
                UPDATE rate_control.rate_windows AS rate_window
                   SET observed_max = GREATEST(rate_window.observed_max, observed_value),
                       updated_at = clock_timestamp()
                  FROM rate_control.allocations AS allocation
                 WHERE allocation.window_id = rate_window.window_id
                   AND allocation.permit_id = NEW.permit_id
                   AND rate_window.rate_limit_type = 'ORDERS'
                   AND rate_window.interval_name = interval_name_value;
            END LOOP;

            status_code := (NEW.payload->>'http_status')::integer;
            IF status_code NOT IN (418, 429) THEN
                RETURN NEW;
            END IF;
            IF status_code = 418 THEN
                INSERT INTO rate_control.authority_blocks(
                  endpoint_authority,blocked_until,reason_code,
                  consecutive_backoff_count,source_message_id,updated_at
                ) VALUES (
                  NEW.endpoint_authority,NULL,'HTTP_418_INDEFINITE',0,NEW.message_id,
                  clock_timestamp()
                ) ON CONFLICT (endpoint_authority) DO UPDATE SET
                  blocked_until=NULL, reason_code='HTTP_418_INDEFINITE',
                  consecutive_backoff_count=0, source_message_id=EXCLUDED.source_message_id,
                  updated_at=clock_timestamp();
                RETURN NEW;
            END IF;

            retry_seconds := (NEW.payload->>'retry_after_seconds')::integer;
            SELECT consecutive_backoff_count INTO current_count
              FROM rate_control.authority_blocks
             WHERE endpoint_authority = NEW.endpoint_authority FOR UPDATE;
            current_count := COALESCE(current_count, 0);
            IF retry_seconds IS NULL THEN
                retry_seconds := LEAST(900, 60 * (2 ^ LEAST(current_count, 4))::integer);
                current_count := current_count + 1;
            ELSE
                current_count := 0;
            END IF;
            SELECT GREATEST(
                     clock_timestamp() + make_interval(secs => retry_seconds),
                     COALESCE(MAX(window_end) + interval '250 milliseconds',
                              clock_timestamp())
                   ) INTO computed_block_until
              FROM rate_control.rate_windows
             WHERE endpoint_authority = NEW.endpoint_authority
               AND window_end > clock_timestamp();
            INSERT INTO rate_control.authority_blocks(
              endpoint_authority,blocked_until,reason_code,
              consecutive_backoff_count,source_message_id,updated_at
            ) VALUES (
              NEW.endpoint_authority,computed_block_until,'HTTP_429_BACKOFF',
              current_count,NEW.message_id,clock_timestamp()
            ) ON CONFLICT (endpoint_authority) DO UPDATE SET
              blocked_until=CASE
                WHEN rate_control.authority_blocks.blocked_until IS NULL THEN NULL
                ELSE GREATEST(rate_control.authority_blocks.blocked_until,
                              EXCLUDED.blocked_until)
              END,
              reason_code=CASE
                WHEN rate_control.authority_blocks.blocked_until IS NULL
                  THEN rate_control.authority_blocks.reason_code
                ELSE EXCLUDED.reason_code
              END,
              consecutive_backoff_count=EXCLUDED.consecutive_backoff_count,
              source_message_id=EXCLUDED.source_message_id,
              updated_at=clock_timestamp();
            RETURN NEW;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'header reconciliation failed closed: %', SQLSTATE;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER observations_header_reconciliation
          AFTER INSERT ON rate_control.observations
          FOR EACH ROW EXECUTE FUNCTION rate_control.reconcile_header_observation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER observations_header_reconciliation ON rate_control.observations")
    op.execute("DROP FUNCTION rate_control.reconcile_header_observation()")
