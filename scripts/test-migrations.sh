#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="aiq-m0-migrations-$$"
APP_IMAGE="aiq-app:migration-test-$$"
TMP="$(mktemp -d /tmp/aiq-m0-migrations.XXXXXX)"
COMPOSE=(docker compose -p "$RUN_ID" -f "$ROOT/deploy/compose.test.yaml")

cleanup() {
  TEST_BUSINESS_DB_PASSWORD_FILE="$TMP/business" \
  TEST_HOST_DB_PASSWORD_FILE="$TMP/host" \
    "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$APP_IMAGE" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

umask 077
openssl rand -hex 24 > "$TMP/business"
openssl rand -hex 24 > "$TMP/host"
BUSINESS_PASSWORD="$(<"$TMP/business")"
HOST_PASSWORD="$(<"$TMP/host")"
export TEST_BUSINESS_DB_PASSWORD_FILE="$TMP/business"
export TEST_HOST_DB_PASSWORD_FILE="$TMP/host"

docker build --pull=false --provenance=false \
  -f "$ROOT/docker/app.Dockerfile" -t "$APP_IMAGE" "$ROOT"

"${COMPOSE[@]}" up -d --wait business-postgres host-postgres redis

printf 'postgresql+psycopg://aiq_business_test:%s@business-postgres:5432/aiq_business_test' \
  "$BUSINESS_PASSWORD" > "$TMP/business-dsn"
printf 'postgresql+psycopg://aiq_host_control_test:%s@host-postgres:5432/aiq_host_rate_control_test' \
  "$HOST_PASSWORD" > "$TMP/host-dsn"
chown 65532:65532 "$TMP/business-dsn" "$TMP/host-dsn"
chmod 0400 "$TMP/business-dsn" "$TMP/host-dsn"

cd "$ROOT"
run_business_migration() {
  docker run --rm --network "${RUN_ID}_test_db_net" \
    --mount "type=bind,src=$TMP/business-dsn,dst=/run/secrets/business-dsn,readonly" \
    -e AIQ_BUSINESS_DATABASE_URL_FILE=/run/secrets/business-dsn \
    "$APP_IMAGE" alembic -c migrations/business/alembic.ini "$@"
}
run_host_migration() {
  docker run --rm --network "${RUN_ID}_test_db_net" \
    --mount "type=bind,src=$TMP/host-dsn,dst=/run/secrets/host-dsn,readonly" \
    -e AIQ_HOST_CONTROL_DATABASE_URL_FILE=/run/secrets/host-dsn \
    "$APP_IMAGE" alembic -c migrations/host_control/alembic.ini "$@"
}
run_copy_repository_tests() {
  docker run --rm --network "${RUN_ID}_test_db_net" \
    --mount "type=bind,src=$TMP/business-dsn,dst=/run/secrets/business-dsn,readonly" \
    --mount "type=bind,src=$ROOT/scripts/test-copy-repository-postgres.py,dst=/tmp/test-copy-repository-postgres.py,readonly" \
    "$APP_IMAGE" python /tmp/test-copy-repository-postgres.py \
      --database-url-file /run/secrets/business-dsn
}

run_business_migration upgrade head
run_host_migration upgrade head
run_business_migration downgrade base
run_host_migration downgrade base
run_business_migration upgrade head
run_host_migration upgrade head

printf 'migration round-trip PASS\n'

run_copy_repository_tests

docker exec "${RUN_ID}-business-postgres-1" psql -U aiq_business_test -d aiq_business_test -Atc \
  "SELECT state || ':' || new_entries_allowed FROM control.runtime_state" | grep -qx 'RISK_LOCKED:false'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT epoch || ':' || allocator_instance_id FROM rate_control.fencing_state" | grep -qx '1:UNINITIALIZED'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT (measurement->'database_authority'->>'migration_head')||':'||
          (measurement->'nonce_permit_integrity'->>'duplicate_capability_nonce_count')||':'||
          (measurement->'nonce_permit_integrity'->>'consumed_without_gateway_count')||':'||
          (measurement->'nonce_permit_integrity'->>'outcome_missing_past_deadline_count')||':'||
          jsonb_array_length(measurement->'active_authority_blocks')
     FROM (SELECT rate_control.read_startup_measurements() AS measurement) AS snapshot" \
  | grep -qx '0010_local_measurements:0:0:0:0'
docker exec "${RUN_ID}-business-postgres-1" psql -U aiq_business_test -d aiq_business_test -Atc \
  "SELECT extversion FROM pg_extension WHERE extname='timescaledb'" | grep -Eq '^2\.'
docker exec "${RUN_ID}-business-postgres-1" psql -U aiq_business_test -d aiq_business_test -Atc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='market'
      AND table_name IN (
        'raw_archive_objects','remote_archive_receipts','data_manifests',
        'data_quality_intervals'
      )" | grep -qx '4'
docker exec "${RUN_ID}-business-postgres-1" psql -U aiq_business_test -d aiq_business_test -Atc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='trading'
      AND table_name IN (
        'risk_decisions','risk_reservation_events','order_intents','account_snapshots',
        'position_snapshots','protection_observations'
      )" | grep -qx '6'
docker exec "${RUN_ID}-business-postgres-1" psql -U aiq_business_test -d aiq_business_test -Atc \
  "SELECT count(*) FROM information_schema.tables
    WHERE table_schema='control'
      AND table_name IN (
        'command_requests','command_events','flatten_challenges',
        'flatten_challenge_consumptions','incident_events','notification_deliveries',
        'backup_manifests'
      )" | grep -qx '7'
docker exec "${RUN_ID}-redis-1" redis-cli ping | grep -qx PONG

printf 'database invariants PASS\n'

docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT rolcanlogin||':'||rolsuper||':'||rolcreatedb||':'||rolcreaterole||':'||
          rolreplication||':'||rolbypassrls
     FROM pg_roles WHERE rolname='aiq_rate_authority'" \
  | grep -qx 'false:false:false:false:false:false'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT has_schema_privilege('aiq_rate_authority','rate_control','USAGE')||':'||
          has_table_privilege('aiq_rate_authority','rate_control.rate_windows','SELECT')||':'||
          has_table_privilege('aiq_rate_authority','rate_control.observations','SELECT')||':'||
          has_table_privilege('aiq_rate_authority','rate_control.authority_blocks','SELECT')||':'||
          has_table_privilege(
            'aiq_rate_authority','rate_control.reservation_decisions','INSERT')||':'||
          has_function_privilege(
            'aiq_rate_authority',
            'rate_control.acquire_fencing_lease(character varying,bigint,integer)',
            'EXECUTE')||':'||
          has_function_privilege(
            'aiq_rate_authority',
            'rate_control.read_startup_measurements()',
            'EXECUTE')||':'||
          has_function_privilege(
            'aiq_rate_authority',
            'rate_control.read_startup_observations(character varying[],timestamp with time zone)',
            'EXECUTE')||':'||
          (SELECT count(*) FROM pg_proc AS procedure
            JOIN pg_namespace AS namespace ON namespace.oid=procedure.pronamespace
           WHERE namespace.nspname='rate_control'
             AND has_function_privilege('aiq_rate_authority',procedure.oid,'EXECUTE'))" \
  | grep -qx 'true:true:false:false:true:true:true:true:6'
docker exec "${RUN_ID}-host-postgres-1" psql -v ON_ERROR_STOP=1 \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SET ROLE aiq_rate_authority;
   SELECT jsonb_array_length(
     rate_control.read_startup_measurements()->'active_authority_blocks'
   );
   SELECT count(*) FROM rate_control.read_startup_observations(
     ARRAY['BINANCE_PRODUCTION_FAPI']::varchar[], clock_timestamp() - interval '5 seconds'
   );
   RESET ROLE" | grep -E '^[0]$' | wc -l | grep -qx '2'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT bool_and(prosecdef AND proconfig @> ARRAY['search_path=pg_catalog, rate_control'])||':'||
          bool_and(NOT EXISTS (
            SELECT 1
              FROM aclexplode(COALESCE(
                procedure.proacl,acldefault('f',procedure.proowner)
              )) AS privilege
             WHERE privilege.grantee=0 AND privilege.privilege_type='EXECUTE'
          ))
     FROM pg_proc AS procedure
     JOIN pg_namespace AS namespace ON namespace.oid=procedure.pronamespace
    WHERE namespace.nspname='rate_control'" \
  | grep -qx 'true:true'
printf 'least-privilege runtime database role PASS\n'

HASH_A="$(printf 'a%.0s' {1..64})"
HASH_B="$(printf 'b%.0s' {1..64})"
HASH_C="$(printf 'c%.0s' {1..64})"
HASH_D="$(printf 'd%.0s' {1..64})"
HASH_E="$(printf 'e%.0s' {1..64})"
HASH_F="$(printf 'f%.0s' {1..64})"

LEASE_ACQUIRE="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code||':'||fencing_epoch
     FROM rate_control.acquire_fencing_lease('rate-allocator-01',1,300)")"
LEASE_COMPETING="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code||':'||fencing_epoch
     FROM rate_control.acquire_fencing_lease('rate-allocator-02',2,300)")"
LEASE_RENEW="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code||':'||fencing_epoch
     FROM rate_control.acquire_fencing_lease('rate-allocator-01',2,300)")"
test "$LEASE_ACQUIRE" = 'GRANTED:FENCING_LEASE_ACQUIRED:2'
test "$LEASE_COMPETING" = 'DENIED:FENCING_LEASE_HELD:2'
test "$LEASE_RENEW" = 'GRANTED:FENCING_LEASE_RENEWED:2'
FENCING_EPOCH=2
printf 'fencing lease acquire=%s competing=%s renew=%s\n' \
  "$LEASE_ACQUIRE" "$LEASE_COMPETING" "$LEASE_RENEW"

docker exec -i "${RUN_ID}-host-postgres-1" psql -v ON_ERROR_STOP=1 \
  -U aiq_host_control_test -d aiq_host_rate_control_test <<SQL >/dev/null
INSERT INTO rate_control.endpoint_runtime_policies(
  endpoint_authority,endpoint_id,endpoint_catalog_hash,policy_payload_hash,status,
  allowed_callers,derived_operation_class,cost_vector,allowed_operation_classes,
  causal_role_class_map,class_cost_vectors,endpoint_contract_payload,
  endpoint_contract_hash,valid_from,valid_until
) VALUES (
  'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_B','SIGNED_RUNTIME',
  ARRAY['execution-service'],'HOST_RATE_CONTROL',
  '[{"scope_key_hash":"$HASH_C","rate_limit_type":"REQUEST_WEIGHT","interval_name":"MINUTE_1","cost":1,"ceiling_units":10}]',
  ARRAY['HOST_RATE_CONTROL','MARKET_DATA_SNAPSHOT'],
  '{"SYSTEM_LIVENESS":"HOST_RATE_CONTROL","MARKET_DATA_SNAPSHOT":"MARKET_DATA_SNAPSHOT"}',
  '{"HOST_RATE_CONTROL":[{"scope_key_hash":"$HASH_C","rate_limit_type":"REQUEST_WEIGHT","interval_name":"MINUTE_1","cost":1,"ceiling_units":10}],"MARKET_DATA_SNAPSHOT":[{"scope_key_hash":"$HASH_C","rate_limit_type":"REQUEST_WEIGHT","interval_name":"MINUTE_1","cost":2,"ceiling_units":10}]}',
  '{"endpoint_id":"REST_QUERY_TIME"}','$HASH_F',
  now()-interval '1 minute',now()+interval '1 minute'
);
INSERT INTO rate_control.rate_windows(
  endpoint_authority,scope_key_hash,rate_limit_type,interval_name,window_start,
  window_end,effective_used,observed_max,hard_limit,limit_source_hash
) VALUES (
  'BINANCE_PRODUCTION_FAPI','$HASH_C','REQUEST_WEIGHT','MINUTE_1',
  now()-interval '1 minute',now()+interval '1 minute',0,0,10,'$HASH_D'
);
INSERT INTO rate_control.capability_nonces(nonce,payload_hash,state,expires_at)
VALUES ('nonce-1','$HASH_E','RESERVED',now()+interval '1 minute');
INSERT INTO rate_control.permits(
  permit_id,request_key,subject_caller_service,subject_caller_instance_id,
  environment,endpoint_authority,endpoint_id,endpoint_catalog_hash,derived_operation_class,
  canonical_request_hash,parameter_hash,wire_bytes_hash,
  operation_facts_hash,capability_payload_hash,gateway_request_document_hash,
  capability_nonce,fencing_epoch,
  state,reserved_at,expires_at
) VALUES (
  'permit-1','request-1','execution-service','execution-1','production',
  'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','HOST_RATE_CONTROL',
  '$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F','nonce-1',$FENCING_EPOCH,
  'RESERVED',now(),now()+interval '1 minute'
);
SQL

RESERVE_FIRST="$(docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code||':'||permit_id FROM rate_control.reserve_permit(
    'permit-reserve-1','request-reserve-1','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-reserve-1',$FENCING_EPOCH,now()+interval '4 seconds')")"
RESERVE_RETRY="$(docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code||':'||permit_id FROM rate_control.reserve_permit(
    'permit-reserve-ignored','request-reserve-1','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-reserve-1',$FENCING_EPOCH,now()+interval '4 seconds')")"
RESERVE_REPLAY="$(docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit(
    'permit-reserve-2','request-reserve-2','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-reserve-1',$FENCING_EPOCH,now()+interval '4 seconds')")"
printf 'reserve decisions first=%s retry=%s replay=%s\n' \
  "$RESERVE_FIRST" "$RESERVE_RETRY" "$RESERVE_REPLAY"
test "$RESERVE_FIRST" = 'GRANTED:RATE_GRANTED:permit-reserve-1'
test "$RESERVE_RETRY" = 'GRANTED:RATE_GRANTED:permit-reserve-1'
test "$RESERVE_REPLAY" = 'DENIED:RATE_CAPABILITY_REPLAYED'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT effective_used FROM rate_control.rate_windows WHERE scope_key_hash='$HASH_C'" \
  | grep -qx '1'
printf 'atomic reservation and idempotency PASS\n'

RESERVE_V2_FIRST="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code||':'||permit_id||':'||derived_operation_class
     FROM rate_control.reserve_permit_v2(
    'permit-v2-1','request-v2-1','execution-service','execution-1','production',NULL,
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','MARKET_DATA_SNAPSHOT',
    '$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F','nonce-v2-1',
    $FENCING_EPOCH,now()+interval '4 seconds')")"
RESERVE_V2_RETRY="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code||':'||permit_id||':'||derived_operation_class
     FROM rate_control.reserve_permit_v2(
    'permit-v2-ignored','request-v2-1','execution-service','execution-1','production',NULL,
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','MARKET_DATA_SNAPSHOT',
    '$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F','nonce-v2-1',
    $FENCING_EPOCH,now()+interval '4 seconds')")"
RESERVE_V2_CLASS_REPLAY="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit_v2(
    'permit-v2-2','request-v2-1','execution-service','execution-1','production',NULL,
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','HOST_RATE_CONTROL',
    '$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F','nonce-v2-1',
    $FENCING_EPOCH,now()+interval '4 seconds')")"
RESERVE_V2_CLASS_DENIED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit_v2(
    'permit-v2-3','request-v2-3','execution-service','execution-1','production',NULL,
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','ORDER_MUTATION',
    '$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F','nonce-v2-3',
    $FENCING_EPOCH,now()+interval '4 seconds')")"
test "$RESERVE_V2_FIRST" = \
  'GRANTED:RATE_GRANTED:permit-v2-1:MARKET_DATA_SNAPSHOT'
test "$RESERVE_V2_RETRY" = \
  'GRANTED:RATE_GRANTED:permit-v2-1:MARKET_DATA_SNAPSHOT'
test "$RESERVE_V2_CLASS_REPLAY" = 'DENIED:RATE_CAPABILITY_REPLAYED'
test "$RESERVE_V2_CLASS_DENIED" = 'DENIED:RATE_CAUSAL_ROLE_INVALID'
CONSUME_V2_MISMATCH="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.consume_permit_v2(
    'permit-v2-1','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME',NULL,'$HASH_A','$HASH_A','$HASH_F',
    '$HASH_C','$HASH_D','$HASH_E','$HASH_F',$FENCING_EPOCH,'gateway-v2')")"
CONSUME_V2_GRANTED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.consume_permit_v2(
    'permit-v2-1','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME',NULL,'$HASH_A','$HASH_A','$HASH_B',
    '$HASH_C','$HASH_D','$HASH_E','$HASH_F',$FENCING_EPOCH,'gateway-v2')")"
CONSUME_V2_REPEAT="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.consume_permit_v2(
    'permit-v2-1','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME',NULL,'$HASH_A','$HASH_A','$HASH_B',
    '$HASH_C','$HASH_D','$HASH_E','$HASH_F',$FENCING_EPOCH,'gateway-v2')")"
test "$CONSUME_V2_MISMATCH" = 'CONSUME_DENIED:RATE_PARAMETER_HASH_MISMATCH'
test "$CONSUME_V2_GRANTED" = 'CONSUME_GRANTED:RATE_PERMIT_CONSUMED'
test "$CONSUME_V2_REPEAT" = 'CONSUME_DENIED:RATE_PERMIT_ALREADY_CONSUMED'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT effective_used FROM rate_control.rate_windows WHERE scope_key_hash='$HASH_C'" \
  | grep -qx '3'
printf 'multi-class reservation first=%s retry=%s replay=%s denied=%s\n' \
  "$RESERVE_V2_FIRST" "$RESERVE_V2_RETRY" \
  "$RESERVE_V2_CLASS_REPLAY" "$RESERVE_V2_CLASS_DENIED"
printf 'full-bind consume mismatch=%s granted=%s repeat=%s\n' \
  "$CONSUME_V2_MISMATCH" "$CONSUME_V2_GRANTED" "$CONSUME_V2_REPEAT"

RESERVE_CALLER_DENIED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit(
    'permit-denied-1','request-denied-1','trading-engine','trading-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-denied-1',$FENCING_EPOCH,now()+interval '4 seconds')")"
RESERVE_FENCING_DENIED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit(
    'permit-denied-2','request-denied-2','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-denied-2',3,now()+interval '4 seconds')")"
RESERVE_CATALOG_DENIED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit(
    'permit-denied-3','request-denied-3','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_F','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-denied-3',$FENCING_EPOCH,now()+interval '4 seconds')")"
test "$RESERVE_CALLER_DENIED" = 'DENIED:RATE_CALLER_NOT_ALLOWED'
test "$RESERVE_FENCING_DENIED" = 'DENIED:RATE_FENCING_STALE'
test "$RESERVE_CATALOG_DENIED" = 'DENIED:RATE_ENDPOINT_UNKNOWN'

docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "UPDATE rate_control.rate_windows SET blocked_until=now()+interval '1 minute' WHERE scope_key_hash='$HASH_C'" \
  >/dev/null
RESERVE_BLOCKED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit(
    'permit-denied-4','request-denied-4','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-denied-4',$FENCING_EPOCH,now()+interval '4 seconds')")"
test "$RESERVE_BLOCKED" = 'DENIED:RATE_SCOPE_BLOCKED'
printf 'reservation denial gates caller=%s fencing=%s catalog=%s blocked=%s\n' \
  "$RESERVE_CALLER_DENIED" "$RESERVE_FENCING_DENIED" \
  "$RESERVE_CATALOG_DENIED" "$RESERVE_BLOCKED"

FIRST_CONSUME="$(docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.consume_permit(
    'permit-1','$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F',$FENCING_EPOCH,'gateway-1')")"
SECOND_CONSUME="$(docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.consume_permit(
    'permit-1','$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F',$FENCING_EPOCH,'gateway-1')")"
printf 'permit consume decisions first=%s second=%s\n' "$FIRST_CONSUME" "$SECOND_CONSUME"
test "$FIRST_CONSUME" = 'CONSUME_GRANTED:RATE_PERMIT_CONSUMED'
test "$SECOND_CONSUME" = 'CONSUME_DENIED:PERMIT_NOT_RESERVED'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT p.state||':'||n.state FROM rate_control.permits p JOIN rate_control.capability_nonces n ON n.nonce=p.capability_nonce WHERE p.permit_id='permit-1'" \
  | grep -qx 'CONSUMED:CONSUMED'

printf 'permit and nonce terminal states PASS\n'

docker exec -i "${RUN_ID}-host-postgres-1" psql -v ON_ERROR_STOP=1 \
  -U aiq_host_control_test -d aiq_host_rate_control_test <<SQL >/dev/null
INSERT INTO rate_control.reservation_decisions(
  message_id,request_message_id,request_key,decision,reason_code,permit_id,
  caller_service,caller_instance_id,endpoint_authority,endpoint_id,
  derived_operation_class,endpoint_catalog_hash,operation_facts_hash,
  capability_payload_hash,fencing_epoch,peer_pid,peer_uid,peer_gid,occurred_at
) VALUES (
  'audit-reserve-decision-0001','audit-reserve-request-0001','request-reserve-1',
  'GRANTED','RATE_GRANTED','permit-reserve-1','execution-service','execution-1',
  'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','HOST_RATE_CONTROL','$HASH_A','$HASH_D',
  '$HASH_E',$FENCING_EPOCH,123,11002,11002,now()
);
INSERT INTO rate_control.consume_decisions(
  message_id,request_message_id,permit_id,decision,reason_code,gateway_instance_id,
  canonical_request_hash,parameter_hash,wire_bytes_hash,operation_facts_hash,
  capability_payload_hash,request_document_hash,fencing_epoch,send_deadline,
  peer_pid,peer_uid,peer_gid,occurred_at
) VALUES (
  'audit-consume-decision-0001','audit-consume-request-0001','permit-1',
  'CONSUME_GRANTED','RATE_PERMIT_CONSUMED','gateway-1','$HASH_A','$HASH_B','$HASH_C',
  '$HASH_D','$HASH_E','$HASH_F',$FENCING_EPOCH,now()+interval '50 milliseconds',
  124,11005,11005,now()
);
SQL
if docker exec "${RUN_ID}-host-postgres-1" psql -v ON_ERROR_STOP=1 \
  -U aiq_host_control_test -d aiq_host_rate_control_test -c \
  "DELETE FROM rate_control.consume_decisions" >/dev/null 2>&1; then
  printf 'append-only consume decision deletion unexpectedly succeeded\n' >&2
  exit 1
fi
printf 'append-only reserve and consume decision journals PASS\n'

SEND_OUTCOME_PAYLOAD="$(jq -cn \
  --arg message_id 'gateway-outcome-0001' \
  --arg permit_id 'permit-1' \
  --arg gateway 'gateway-1' \
  --arg canonical "$HASH_A" \
  --argjson epoch "$FENCING_EPOCH" \
  '{message_id:$message_id,message_type:"SendOutcome",
    occurred_at:"2026-07-14T00:00:00Z",caller_instance_id:$gateway,
    permit_id:$permit_id,canonical_request_hash:$canonical,fencing_epoch:$epoch,
    outcome:"SENT_UNKNOWN"}')"
OUTCOME_RECORDED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.record_gateway_message(
    \$json\$$SEND_OUTCOME_PAYLOAD\$json\$::jsonb,'$HASH_A')")"
OUTCOME_RETRY="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.record_gateway_message(
    \$json\$$SEND_OUTCOME_PAYLOAD\$json\$::jsonb,'$HASH_A')")"
OUTCOME_REPLAY="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.record_gateway_message(
    \$json\$$SEND_OUTCOME_PAYLOAD\$json\$::jsonb,'$HASH_B')")"
HEADER_PAYLOAD="$(jq -cn \
  --arg message_id 'gateway-header-0001' \
  --arg permit_id 'permit-v2-1' \
  --arg gateway 'gateway-v2' \
  --arg authority 'BINANCE_PRODUCTION_FAPI' \
  --argjson epoch "$FENCING_EPOCH" \
  '{message_id:$message_id,message_type:"HeaderObservation",
    occurred_at:"2026-07-14T00:00:01Z",caller_instance_id:$gateway,
    permit_id:$permit_id,endpoint_authority:$authority,fencing_epoch:$epoch,
    used_weight_observations:{"x-mbx-used-weight-1m":8},
    order_count_observations:{},retry_after_seconds:30,http_status:429}')"
OBSERVATION_RECORDED="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.record_gateway_message(
    \$json\$$HEADER_PAYLOAD\$json\$::jsonb,'$HASH_B')")"
test "$OUTCOME_RECORDED" = 'RECORDED:RATE_GATEWAY_EVENT_RECORDED'
test "$OUTCOME_RETRY" = 'RECORDED:RATE_GATEWAY_EVENT_IDEMPOTENT'
test "$OUTCOME_REPLAY" = 'DENIED:RATE_GATEWAY_EVENT_REPLAYED'
test "$OBSERVATION_RECORDED" = 'RECORDED:RATE_GATEWAY_EVENT_RECORDED'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT (SELECT count(*) FROM rate_control.send_outcomes)||':'||
          (SELECT count(*) FROM rate_control.observations)" | grep -qx '1:1'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT observed_max FROM rate_control.rate_windows WHERE scope_key_hash='$HASH_C'" \
  | grep -qx '8'
docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "SELECT reason_code||':'||(blocked_until > now())
     FROM rate_control.authority_blocks
    WHERE endpoint_authority='BINANCE_PRODUCTION_FAPI'" \
  | grep -qx 'HTTP_429_BACKOFF:true'
if docker exec "${RUN_ID}-host-postgres-1" psql -v ON_ERROR_STOP=1 \
  -U aiq_host_control_test -d aiq_host_rate_control_test -c \
  "UPDATE rate_control.send_outcomes SET outcome='NOT_SENT'" >/dev/null 2>&1; then
  printf 'append-only send outcome mutation unexpectedly succeeded\n' >&2
  exit 1
fi
printf 'gateway journal outcome=%s retry=%s replay=%s observation=%s\n' \
  "$OUTCOME_RECORDED" "$OUTCOME_RETRY" "$OUTCOME_REPLAY" "$OBSERVATION_RECORDED"

docker exec "${RUN_ID}-host-postgres-1" psql -U aiq_host_control_test \
  -d aiq_host_rate_control_test -Atc \
  "UPDATE rate_control.fencing_state
      SET lease_acquired_at=now()-interval '3 minutes',
          lease_renewed_at=now()-interval '2 minutes',
          lease_expires_at=now()-interval '1 minute'
    WHERE singleton=true" >/dev/null
EXPIRED_LEASE_RESERVE="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit(
    'permit-expired-lease','request-expired-lease','execution-service','execution-1','production',
    'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A','$HASH_A','$HASH_B','$HASH_C',
    '$HASH_D','$HASH_E','$HASH_F','nonce-expired-lease',$FENCING_EPOCH,
    now()+interval '4 seconds')")"
EXPIRED_LEASE_RESERVE_V2="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.reserve_permit_v2(
    'permit-v2-expired-lease','request-v2-expired-lease','execution-service',
    'execution-1','production',NULL,'BINANCE_PRODUCTION_FAPI','REST_QUERY_TIME','$HASH_A',
    'HOST_RATE_CONTROL','$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F',
    'nonce-v2-expired-lease',$FENCING_EPOCH,now()+interval '4 seconds')")"
EXPIRED_LEASE_CONSUME="$(docker exec "${RUN_ID}-host-postgres-1" psql \
  -U aiq_host_control_test -d aiq_host_rate_control_test -Atc \
  "SELECT decision||':'||reason_code FROM rate_control.consume_permit(
    'permit-missing','$HASH_A','$HASH_B','$HASH_C','$HASH_D','$HASH_E','$HASH_F',
    $FENCING_EPOCH,'gateway-1')")"
test "$EXPIRED_LEASE_RESERVE" = 'DENIED:RATE_FENCING_STALE'
test "$EXPIRED_LEASE_RESERVE_V2" = 'DENIED:RATE_FENCING_STALE'
test "$EXPIRED_LEASE_CONSUME" = 'CONSUME_DENIED:FENCING_EPOCH_MISMATCH'
printf 'expired fencing lease denies reserve=%s reserve_v2=%s consume=%s\n' \
  "$EXPIRED_LEASE_RESERVE" "$EXPIRED_LEASE_RESERVE_V2" "$EXPIRED_LEASE_CONSUME"

printf 'migration integration PASS project=%s\n' "$RUN_ID"
