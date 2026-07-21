"""Create a non-login least-privilege role for the rate authority runtime."""

from alembic import op

revision = "0009_runtime_role"
down_revision = "0008_decision_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aiq_rate_authority') THEN
                RAISE EXCEPTION 'aiq_rate_authority already exists';
            END IF;
            CREATE ROLE aiq_rate_authority
              NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
              NOREPLICATION NOBYPASSRLS;
            EXECUTE format(
              'GRANT CONNECT ON DATABASE %I TO aiq_rate_authority', current_database()
            );
        END $$;
        REVOKE ALL ON SCHEMA rate_control FROM PUBLIC;
        REVOKE ALL ON ALL TABLES IN SCHEMA rate_control FROM PUBLIC;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA rate_control FROM PUBLIC;
        REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA rate_control FROM PUBLIC;
        GRANT USAGE ON SCHEMA rate_control TO aiq_rate_authority;
        GRANT SELECT ON ALL TABLES IN SCHEMA rate_control TO aiq_rate_authority;
        GRANT INSERT ON rate_control.reservation_decisions,
                        rate_control.consume_decisions TO aiq_rate_authority;
        GRANT USAGE, SELECT ON SEQUENCE
          rate_control.reservation_decisions_decision_id_seq,
          rate_control.consume_decisions_decision_id_seq TO aiq_rate_authority;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rate_control TO aiq_rate_authority;
        ALTER DEFAULT PRIVILEGES IN SCHEMA rate_control
          REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
        ALTER DEFAULT PRIVILEGES IN SCHEMA rate_control
          REVOKE ALL ON TABLES FROM PUBLIC;
        ALTER DEFAULT PRIVILEGES IN SCHEMA rate_control
          REVOKE ALL ON SEQUENCES FROM PUBLIC;
        """
    )
    op.execute(
        """
        DO $$
        DECLARE
            function_identity text;
        BEGIN
            FOR function_identity IN
                SELECT procedure.oid::regprocedure::text
                  FROM pg_proc AS procedure
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = procedure.pronamespace
                 WHERE namespace.nspname = 'rate_control'
            LOOP
                EXECUTE format(
                  'ALTER FUNCTION %s SECURITY DEFINER SET search_path TO pg_catalog, rate_control',
                  function_identity
                );
            END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            function_identity text;
        BEGIN
            FOR function_identity IN
                SELECT procedure.oid::regprocedure::text
                  FROM pg_proc AS procedure
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = procedure.pronamespace
                 WHERE namespace.nspname = 'rate_control'
            LOOP
                EXECUTE format(
                  'ALTER FUNCTION %s SECURITY INVOKER RESET ALL',
                  function_identity
                );
            END LOOP;
        END $$;
        GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA rate_control TO PUBLIC;
        ALTER DEFAULT PRIVILEGES IN SCHEMA rate_control
          GRANT EXECUTE ON FUNCTIONS TO PUBLIC;
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA rate_control
          FROM aiq_rate_authority;
        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA rate_control
          FROM aiq_rate_authority;
        REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA rate_control
          FROM aiq_rate_authority;
        REVOKE USAGE ON SCHEMA rate_control FROM aiq_rate_authority;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            EXECUTE format(
              'REVOKE CONNECT ON DATABASE %I FROM aiq_rate_authority', current_database()
            );
            DROP ROLE aiq_rate_authority;
        END $$;
        """
    )
