"""PostgreSQL-backed atomic copy-order submission journal."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_quant.copy_trading.execution import CopyOrderType, SubmissionClaim, SubmissionEvent


class SubmissionJournalError(RuntimeError):
    """Submission ownership could not be durably established."""


class PostgresSubmissionJournal:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("copy submission database DSN is required")
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def lookup(self, *, signal_id: str) -> SubmissionClaim | None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT claim.signal_id,
                           coalesce(upgrade.client_order_id,claim.client_order_id)
                             AS client_order_id,
                           claim.request_hash,
                           CASE WHEN upgrade.signal_id IS NULL
                                THEN claim.request_hash_version ELSE 1 END
                             AS request_hash_version,
                           claim.requested_quantity,claim.leverage,
                           claim.order_type,claim.limit_price,
                           CASE WHEN upgrade.signal_id IS NULL
                                THEN claim.expires_at ELSE NULL END AS expires_at,
                           coalesce(upgrade.occurred_at,claim.claimed_at) AS claimed_at,
                           upgrade.signal_id IS NOT NULL AS policy_upgraded
                      FROM copytrading.submission_claims AS claim
                      LEFT JOIN copytrading.submission_policy_upgrade_events AS upgrade
                        USING(signal_id)
                     WHERE claim.signal_id=%s
                    """,
                    (signal_id,),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise SubmissionJournalError("COPY_SUBMISSION_LOOKUP_UNAVAILABLE") from error
        if row is None:
            return None
        return SubmissionClaim(
            signal_id=str(row["signal_id"]),
            client_order_id=str(row["client_order_id"]),
            request_hash=str(row["request_hash"]),
            request_hash_version=int(row["request_hash_version"]),
            requested_quantity=(
                None
                if row["requested_quantity"] is None
                else Decimal(str(row["requested_quantity"]))
            ),
            leverage=None if row["leverage"] is None else int(row["leverage"]),
            order_type=CopyOrderType(str(row["order_type"])),
            limit_price=(None if row["limit_price"] is None else Decimal(str(row["limit_price"]))),
            expires_at=row["expires_at"],
            claimed_at=row["claimed_at"],
            policy_upgraded=bool(row["policy_upgraded"]),
        )

    def claim(
        self,
        *,
        signal_id: str,
        client_order_id: str,
        request_hash: str,
        request_hash_version: int,
        requested_quantity: Decimal,
        leverage: int,
        order_type: CopyOrderType,
        limit_price: Decimal | None,
        expires_at: datetime | None,
        claimed_at: datetime,
        submitting_event: SubmissionEvent,
    ) -> bool:
        if submitting_event.signal_id != signal_id or submitting_event.state != "SUBMITTING":
            raise ValueError("copy submitting event does not match its claim")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.submission_claims(
                      signal_id,client_order_id,request_hash,requested_quantity,
                      leverage,order_type,limit_price,expires_at,claimed_at,
                      request_hash_version
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    RETURNING signal_id
                    """,
                    (
                        signal_id,
                        client_order_id,
                        request_hash,
                        requested_quantity,
                        leverage,
                        order_type.value,
                        limit_price,
                        expires_at,
                        claimed_at,
                        request_hash_version,
                    ),
                )
                if cursor.fetchone() is None:
                    return False
                cursor.execute(
                    """
                    INSERT INTO copytrading.submission_events(
                      submission_event_id,signal_id,state,filled_quantity,
                      exchange_order_id,response_hash,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        submitting_event.event_id,
                        submitting_event.signal_id,
                        submitting_event.state,
                        submitting_event.filled_quantity,
                        submitting_event.exchange_order_id,
                        submitting_event.response_hash,
                        Jsonb(list(submitting_event.reason_codes)),
                        submitting_event.occurred_at,
                    ),
                )
                return True
        except psycopg.Error as error:
            raise SubmissionJournalError("COPY_SUBMISSION_CLAIM_UNAVAILABLE") from error

    def record(self, event: SubmissionEvent) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.submission_events(
                      submission_event_id,signal_id,state,filled_quantity,
                      exchange_order_id,response_hash,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (submission_event_id) DO NOTHING
                    """,
                    (
                        event.event_id,
                        event.signal_id,
                        event.state,
                        event.filled_quantity,
                        event.exchange_order_id,
                        event.response_hash,
                        Jsonb(list(event.reason_codes)),
                        event.occurred_at,
                    ),
                )
        except psycopg.Error as error:
            raise SubmissionJournalError("COPY_SUBMISSION_EVENT_UNAVAILABLE") from error
