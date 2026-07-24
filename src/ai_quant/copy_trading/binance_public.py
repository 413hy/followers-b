"""Exact-destination client for Binance's public copy-trading web data."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from ai_quant.copy_trading.models import (
    LeaderSnapshot,
    PublicCopyDataError,
    PublicLeaderOrder,
    SourcePositionSide,
)

BINANCE_WEB_BASE = "https://www.binance.com"
LEADER_LIST_PATH = "/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
LEADER_DETAIL_PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail"
ORDER_HISTORY_PATH = "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history"
POSITION_HISTORY_PATH = (
    "/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history"
)
MAX_PUBLIC_RESPONSE_BYTES = 4 * 1024 * 1024
COPY_ORDER_POLL_PAGE_SIZE = 30
_FAST_LEADER_LOOKUP_RANKINGS = ("AUM", "WIN_RATE", "MDD")


class BinancePublicCopyError(RuntimeError):
    """A public-data request failed without retaining an untrusted response body."""


@dataclass(frozen=True, slots=True)
class PublicHttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class LeaderPage:
    leaders: tuple[LeaderSnapshot, ...]
    total: int
    invalid_row_count: int = 0
    invalid_reason_codes: tuple[str, ...] = ()
    invalid_leader_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LeaderAvailability:
    """Direct evidence returned by one public lead-portfolio detail lookup."""

    lead_portfolio_id: str
    state: str
    source_status: str
    nickname: str | None = None


@dataclass(frozen=True, slots=True)
class OrderHistoryPage:
    orders: tuple[PublicLeaderOrder, ...]
    total: int


@dataclass(frozen=True, slots=True)
class ClosedLeaderPosition:
    """A public position interval that may still be open at its evidence watermark."""

    symbol: str
    position_side: SourcePositionSide
    opened_at_ms: int
    closed_at_ms: int | None
    maximum_open_quantity: Decimal
    closed_quantity: Decimal
    evidence_updated_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class PositionHistoryPage:
    positions: tuple[ClosedLeaderPosition, ...]
    total: int


PublicTransport = Callable[[str, str, Mapping[str, str], bytes], PublicHttpResult]
Sleeper = Callable[[float], None]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
) -> PublicHttpResult:
    allowed_post = method == "POST" and url in {
        f"{BINANCE_WEB_BASE}{LEADER_LIST_PATH}",
        f"{BINANCE_WEB_BASE}{ORDER_HISTORY_PATH}",
        f"{BINANCE_WEB_BASE}{POSITION_HISTORY_PATH}",
    }
    allowed_get = method == "GET" and re.fullmatch(
        re.escape(f"{BINANCE_WEB_BASE}{LEADER_DETAIL_PATH}")
        + r"\?portfolioId=[0-9]{10,24}",
        url,
    )
    if not (allowed_post or allowed_get):
        raise BinancePublicCopyError("COPY_PUBLIC_DESTINATION_DENIED")
    request = urllib.request.Request(  # noqa: S310 -- exact HTTPS URL allowlisted above
        url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            payload = response.read(MAX_PUBLIC_RESPONSE_BYTES + 1)
            if len(payload) > MAX_PUBLIC_RESPONSE_BYTES:
                raise BinancePublicCopyError("COPY_PUBLIC_RESPONSE_TOO_LARGE")
            return PublicHttpResult(response.status, dict(response.headers.items()), payload)
    except urllib.error.HTTPError as error:
        payload = error.read(MAX_PUBLIC_RESPONSE_BYTES + 1)
        if len(payload) > MAX_PUBLIC_RESPONSE_BYTES:
            raise BinancePublicCopyError("COPY_PUBLIC_RESPONSE_TOO_LARGE") from error
        return PublicHttpResult(error.code, dict(error.headers.items()), payload)
    except (OSError, TimeoutError, urllib.error.URLError) as error:
        raise BinancePublicCopyError("COPY_PUBLIC_TRANSPORT_FAILED") from error


class BinancePublicCopyClient:
    """Read public leader rankings and order history with a replaceable adapter boundary.

    These webpage BAPI endpoints are not a documented trading API. Any contract drift therefore
    fails closed and cannot reach the exchange execution adapter.
    """

    def __init__(
        self,
        *,
        transport: PublicTransport = _urllib_transport,
        retry_attempts: int = 3,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if not 1 <= retry_attempts <= 5:
            raise ValueError("copy public retry count is outside the supported range")
        self._transport = transport
        self._retry_attempts = retry_attempts
        self._sleeper = sleeper

    def leader_availability(self, lead_portfolio_id: str) -> LeaderAvailability:
        """Check one ID directly instead of inferring absence from a ranked directory."""

        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("copy leader portfolio ID is invalid")
        query = urlencode({"portfolioId": lead_portfolio_id})
        result = self._get_result(
            f"{LEADER_DETAIL_PATH}?{query}",
            "COPY_LEADER_DETAIL",
        )
        try:
            response = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BinancePublicCopyError("COPY_LEADER_DETAIL_INVALID_JSON") from error
        if not isinstance(response, dict):
            raise BinancePublicCopyError("COPY_LEADER_DETAIL_INVALID_RESPONSE")
        if result.status == 400:
            if (
                response.get("success") is False
                and response.get("code") == "000002"
                and response.get("message") == "illegal parameter"
                and response.get("data") is None
            ):
                return LeaderAvailability(
                    lead_portfolio_id=lead_portfolio_id,
                    state="MISSING",
                    source_status="NOT_FOUND",
                )
            raise BinancePublicCopyError("COPY_LEADER_DETAIL_HTTP_400")
        if not 200 <= result.status < 300:
            raise BinancePublicCopyError(f"COPY_LEADER_DETAIL_HTTP_{result.status}")
        if response.get("success") is not True or response.get("code") != "000000":
            raise BinancePublicCopyError("COPY_LEADER_DETAIL_API_REJECTED")
        data = response.get("data")
        if not isinstance(data, dict):
            raise BinancePublicCopyError("COPY_LEADER_DETAIL_INVALID_RESPONSE")
        returned_id = data.get("leadPortfolioId")
        status = data.get("status")
        nickname = data.get("nickname")
        if (
            returned_id != lead_portfolio_id
            or not isinstance(status, str)
            or not isinstance(nickname, str)
            or not nickname.strip()
        ):
            raise BinancePublicCopyError("COPY_LEADER_DETAIL_IDENTITY_INVALID")
        if status == "ACTIVE":
            state = "AVAILABLE"
        elif status in {"CLOSING", "CLOSED"}:
            state = "MISSING"
        else:
            raise BinancePublicCopyError("COPY_LEADER_DETAIL_STATUS_UNKNOWN")
        return LeaderAvailability(
            lead_portfolio_id=lead_portfolio_id,
            state=state,
            source_status=status,
            nickname=nickname.strip(),
        )

    def list_leaders(
        self,
        *,
        page_number: int = 1,
        page_size: int = 50,
        time_range: str = "30D",
        data_type: str = "ROI",
        skip_invalid_rows: bool = False,
    ) -> LeaderPage:
        if not 1 <= page_number <= 10_000 or not 1 <= page_size <= 100:
            raise ValueError("copy leader page is outside the supported range")
        if time_range not in {"7D", "30D", "90D"}:
            raise ValueError("copy leader time range is unsupported")
        if data_type not in {"ROI", "PNL", "AUM", "MDD", "WIN_RATE"}:
            raise ValueError("copy leader ranking type is unsupported")
        rows, total = self._leader_rows(
            page_number=page_number,
            page_size=page_size,
            time_range=time_range,
            data_type=data_type,
        )
        leaders: list[LeaderSnapshot] = []
        invalid_reasons: list[str] = []
        invalid_leader_ids: list[str] = []
        for row in rows:
            try:
                leaders.append(LeaderSnapshot.from_api(_require_mapping(row)))
            except PublicCopyDataError as error:
                if not skip_invalid_rows:
                    raise BinancePublicCopyError(str(error)) from error
                invalid_reasons.append(str(error))
                if isinstance(row, Mapping):
                    leader_id = str(row.get("leadPortfolioId", ""))
                    if re.fullmatch(r"[0-9]{10,24}", leader_id):
                        invalid_leader_ids.append(leader_id)
        return LeaderPage(
            leaders=tuple(leaders),
            total=total,
            invalid_row_count=len(invalid_reasons),
            invalid_reason_codes=tuple(sorted(set(invalid_reasons))),
            invalid_leader_ids=tuple(sorted(set(invalid_leader_ids))),
        )

    def list_all_leaders(
        self,
        *,
        time_range: str = "30D",
        data_type: str = "ROI",
        maximum_pages: int = 400,
    ) -> LeaderPage:
        """Read every page while tolerating a bounded live-directory snapshot change."""

        if not 1 <= maximum_pages <= 1000:
            raise ValueError("copy leader directory page limit is invalid")
        for attempt in range(self._retry_attempts):
            try:
                return self._list_all_leaders_snapshot(
                    time_range=time_range,
                    data_type=data_type,
                    maximum_pages=maximum_pages,
                )
            except BinancePublicCopyError as error:
                if (
                    str(error) != "COPY_LEADER_DIRECTORY_INCOMPLETE"
                    or attempt + 1 >= self._retry_attempts
                ):
                    raise
                # Binance's public directory is live and has no snapshot token. If rows
                # move while hundreds of server-sized pages are read, retry the complete
                # snapshot locally instead of surfacing a failed selection run.
                self._retry(attempt)
        raise AssertionError("unreachable public directory retry state")

    def _list_all_leaders_snapshot(
        self,
        *,
        time_range: str,
        data_type: str,
        maximum_pages: int,
    ) -> LeaderPage:
        first = self.list_leaders(
            page_number=1,
            page_size=100,
            time_range=time_range,
            data_type=data_type,
            skip_invalid_rows=True,
        )
        raw_page_size = len(first.leaders) + first.invalid_row_count
        if first.total > 0 and raw_page_size == 0:
            raise BinancePublicCopyError("COPY_LEADER_DIRECTORY_INCOMPLETE")
        latest_total = first.total
        required_pages = (
            0
            if latest_total == 0
            else (latest_total + raw_page_size - 1) // raw_page_size
        )
        if required_pages > maximum_pages:
            raise BinancePublicCopyError("COPY_LEADER_DIRECTORY_PAGE_LIMIT")
        leaders = {leader.lead_portfolio_id: leader for leader in first.leaders}
        invalid_count = first.invalid_row_count
        invalid_reasons = set(first.invalid_reason_codes)
        invalid_leader_ids = set(first.invalid_leader_ids)
        raw_rows_seen = raw_page_size
        page_number = 2
        while page_number <= required_pages:
            page = self.list_leaders(
                page_number=page_number,
                page_size=100,
                time_range=time_range,
                data_type=data_type,
                skip_invalid_rows=True,
            )
            latest_total = page.total
            raw_count = len(page.leaders) + page.invalid_row_count
            if raw_count == 0:
                if raw_rows_seen >= latest_total:
                    break
                raise BinancePublicCopyError("COPY_LEADER_DIRECTORY_INCOMPLETE")
            raw_rows_seen += raw_count
            invalid_count += page.invalid_row_count
            invalid_reasons.update(page.invalid_reason_codes)
            invalid_leader_ids.update(page.invalid_leader_ids)
            for leader in page.leaders:
                leaders[leader.lead_portfolio_id] = leader
            required_pages = (
                0
                if latest_total == 0
                else (latest_total + raw_page_size - 1) // raw_page_size
            )
            if required_pages > maximum_pages:
                raise BinancePublicCopyError("COPY_LEADER_DIRECTORY_PAGE_LIMIT")
            page_number += 1
        if raw_rows_seen < latest_total:
            raise BinancePublicCopyError("COPY_LEADER_DIRECTORY_INCOMPLETE")
        return LeaderPage(
            leaders=tuple(leaders.values()),
            total=latest_total,
            invalid_row_count=invalid_count,
            invalid_reason_codes=tuple(sorted(invalid_reasons)),
            invalid_leader_ids=tuple(sorted(invalid_leader_ids)),
        )

    def find_leader(
        self,
        lead_portfolio_id: str,
        *,
        time_range: str = "30D",
        maximum_pages: int = 100,
    ) -> LeaderSnapshot:
        """Find one public leader without trusting unrelated malformed directory rows."""
        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("copy leader portfolio ID is invalid")
        if time_range not in {"7D", "30D", "90D"} or not 1 <= maximum_pages <= 100:
            raise ValueError("copy leader lookup range is invalid")
        # Detail links can point to a popular leader buried deep in the ROI ranking.
        # Check complementary first pages before retaining the complete ROI fallback.
        for data_type in _FAST_LEADER_LOOKUP_RANKINGS:
            try:
                rows, _ = self._leader_rows(
                    page_number=1,
                    page_size=100,
                    time_range=time_range,
                    data_type=data_type,
                )
            except BinancePublicCopyError:
                continue
            leader = _leader_with_id(rows, lead_portfolio_id)
            if leader is not None:
                return leader
        for page_number in range(1, maximum_pages + 1):
            rows, total = self._leader_rows(
                page_number=page_number,
                page_size=100,
                time_range=time_range,
                data_type="ROI",
            )
            leader = _leader_with_id(rows, lead_portfolio_id)
            if leader is not None:
                return leader
            if page_number * 100 >= total:
                break
        raise BinancePublicCopyError("COPY_LEADER_LOOKUP_NOT_FOUND")

    def search_leaders(
        self,
        nickname_query: str,
        *,
        time_range: str = "30D",
        maximum_results: int = 6,
        maximum_pages: int = 100,
    ) -> tuple[LeaderSnapshot, ...]:
        """Search the current public directory by nickname for Telegram administration."""
        query = nickname_query.strip().casefold()
        if not 2 <= len(query) <= 80 or "\n" in query or "\r" in query:
            raise ValueError("copy leader nickname query is invalid")
        if time_range not in {"7D", "30D", "90D"}:
            raise ValueError("copy leader lookup range is invalid")
        if not 1 <= maximum_results <= 12 or not 1 <= maximum_pages <= 100:
            raise ValueError("copy leader lookup limit is invalid")
        exact: list[LeaderSnapshot] = []
        partial: list[LeaderSnapshot] = []
        seen: set[str] = set()
        for page_number in range(1, maximum_pages + 1):
            rows, total = self._leader_rows(
                page_number=page_number,
                page_size=100,
                time_range=time_range,
                data_type="ROI",
            )
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                nickname = row.get("nickname")
                leader_id = row.get("leadPortfolioId")
                if (
                    not isinstance(nickname, str)
                    or not isinstance(leader_id, str)
                    or leader_id in seen
                    or query not in nickname.casefold()
                ):
                    continue
                try:
                    leader = LeaderSnapshot.from_api(_require_mapping(row))
                except PublicCopyDataError:
                    continue
                seen.add(leader_id)
                (exact if nickname.casefold() == query else partial).append(leader)
            if page_number * 100 >= total:
                break
        return tuple((exact + partial)[:maximum_results])

    def _leader_rows(
        self,
        *,
        page_number: int,
        page_size: int,
        time_range: str,
        data_type: str,
    ) -> tuple[list[object], int]:
        data = self._post(
            LEADER_LIST_PATH,
            {
                "pageNumber": page_number,
                "pageSize": page_size,
                "timeRange": time_range,
                "dataType": data_type,
                "favoriteOnly": False,
                "hideFull": False,
            },
            "COPY_LEADER_LIST",
        )
        rows = data.get("list")
        total = data.get("total")
        if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
            raise BinancePublicCopyError("COPY_LEADER_LIST_INVALID_RESPONSE")
        return rows, total

    def order_history(
        self,
        lead_portfolio_id: str,
        *,
        page_number: int = 1,
        page_size: int = 50,
    ) -> OrderHistoryPage:
        return self._order_history_page(
            lead_portfolio_id,
            page_number=page_number,
            page_size=page_size,
            identity_guard_after_ms=None,
        )

    def order_history_baseline(
        self,
        lead_portfolio_id: str,
        *,
        identity_guard_after_ms: int,
        page_size: int = 100,
    ) -> OrderHistoryPage:
        """Read an initial baseline without rejecting ambiguous older history.

        Binance's public copy history omits the exchange order ID.  An explicitly
        selected leader may therefore have old rows whose derived identities
        collide.  Those rows are safe to persist as a no-trade baseline, while any
        collision at or after the supplied fence remains a hard failure.
        """

        if identity_guard_after_ms <= 0:
            raise ValueError("copy order-history baseline fence is invalid")
        return self._order_history_page(
            lead_portfolio_id,
            page_number=1,
            page_size=page_size,
            identity_guard_after_ms=identity_guard_after_ms,
        )

    def _order_history_page(
        self,
        lead_portfolio_id: str,
        *,
        page_number: int,
        page_size: int,
        identity_guard_after_ms: int | None,
    ) -> OrderHistoryPage:
        if not 1 <= page_number <= 10_000 or not 1 <= page_size <= 100:
            raise ValueError("copy order-history page is outside the supported range")
        data = self._post(
            ORDER_HISTORY_PATH,
            {
                "pageNumber": page_number,
                "pageSize": page_size,
                "portfolioId": lead_portfolio_id,
            },
            "COPY_ORDER_HISTORY",
        )
        rows = data.get("list")
        total = data.get("total")
        if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
            raise BinancePublicCopyError("COPY_ORDER_HISTORY_INVALID_RESPONSE")
        try:
            orders = tuple(
                PublicLeaderOrder.from_api(lead_portfolio_id, _require_mapping(row)) for row in rows
            )
        except PublicCopyDataError as error:
            raise BinancePublicCopyError(str(error)) from error
        orders = _deduplicate_exact_order_events(orders)
        orders = _disambiguate_same_timestamp_batch_orders(orders)
        guarded_orders = (
            orders
            if identity_guard_after_ms is None
            else tuple(
                order for order in orders if order.update_time_ms >= identity_guard_after_ms
            )
        )
        identities = [order.identity_key for order in guarded_orders]
        if len(identities) != len(set(identities)):
            # The public endpoint exposes no exchange order ID. Two rows with the
            # same derived identity cannot be safely distinguished from cumulative
            # updates, so fail closed instead of emitting a guessed quantity.
            raise BinancePublicCopyError("COPY_ORDER_IDENTITY_AMBIGUOUS")
        return OrderHistoryPage(orders=orders, total=total)

    def position_history(
        self,
        lead_portfolio_id: str,
        *,
        page_number: int = 1,
        page_size: int = 50,
    ) -> PositionHistoryPage:
        """Read public position intervals used to resolve one-way orders."""

        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("copy leader portfolio ID is invalid")
        if not 1 <= page_number <= 10_000 or not 1 <= page_size <= 100:
            raise ValueError("copy position-history page is outside the supported range")
        data = self._post(
            POSITION_HISTORY_PATH,
            {
                "pageNumber": page_number,
                "pageSize": page_size,
                "portfolioId": lead_portfolio_id,
            },
            "COPY_POSITION_HISTORY",
        )
        rows = data.get("list")
        total = data.get("total")
        if not isinstance(rows, list) or not isinstance(total, int) or total < 0:
            raise BinancePublicCopyError("COPY_POSITION_HISTORY_INVALID_RESPONSE")
        positions: list[ClosedLeaderPosition] = []
        try:
            for value in rows:
                row = _require_mapping(value)
                symbol = row.get("symbol")
                raw_side = row.get("side")
                opened = row.get("opened")
                closed = row.get("closed")
                status = row.get("status")
                update_time = row.get("updateTime")
                open_interval = closed is None
                if (
                    not isinstance(symbol, str)
                    or not re.fullmatch(r"[A-Z0-9]{3,24}", symbol)
                    or raw_side not in {"Long", "Short"}
                    or not isinstance(opened, int)
                    or isinstance(opened, bool)
                    or opened <= 0
                    or (
                        open_interval
                        and (
                            status not in {"Open", "Partially Closed"}
                            or not isinstance(update_time, int)
                            or isinstance(update_time, bool)
                            or update_time < opened
                        )
                    )
                    or (
                        not open_interval
                        and (
                            not isinstance(closed, int)
                            or isinstance(closed, bool)
                            or closed < opened
                        )
                    )
                ):
                    raise PublicCopyDataError("COPY_POSITION_HISTORY_ROW_INVALID")
                maximum_open_quantity = _position_quantity(
                    row.get("maxOpenInterest"),
                    "maxOpenInterest",
                )
                closed_quantity = _position_quantity(
                    row.get("closedVolume"),
                    "closedVolume",
                    allow_zero=open_interval,
                )
                positions.append(
                    ClosedLeaderPosition(
                        symbol=symbol,
                        position_side=(
                            SourcePositionSide.LONG
                            if raw_side == "Long"
                            else SourcePositionSide.SHORT
                        ),
                        opened_at_ms=opened,
                        closed_at_ms=closed,
                        maximum_open_quantity=maximum_open_quantity,
                        closed_quantity=closed_quantity,
                        evidence_updated_at_ms=(update_time if open_interval else None),
                    )
                )
        except PublicCopyDataError as error:
            raise BinancePublicCopyError(str(error)) from error
        return PositionHistoryPage(positions=tuple(positions), total=total)

    def order_history_since(
        self,
        lead_portfolio_id: str,
        *,
        after_update_time_ms: int,
        page_size: int = 100,
        maximum_pages: int = 10,
    ) -> OrderHistoryPage:
        """Page backwards until the persisted watermark is covered or fail closed."""
        if after_update_time_ms <= 0:
            raise ValueError("copy order-history watermark is invalid")
        if not 1 <= page_size <= 100 or not 1 <= maximum_pages <= 20:
            raise ValueError("copy order-history catch-up limit is invalid")
        for attempt in range(self._retry_attempts):
            try:
                return self._order_history_since_snapshot(
                    lead_portfolio_id,
                    after_update_time_ms=after_update_time_ms,
                    page_size=page_size,
                    maximum_pages=maximum_pages,
                )
            except BinancePublicCopyError as error:
                if (
                    str(error) != "COPY_ORDER_IDENTITY_AMBIGUOUS"
                    or attempt + 1 >= self._retry_attempts
                ):
                    raise
                # The webpage endpoint has no snapshot token. A fill batch can be
                # visible midway through an update, so retry the complete bounded
                # catch-up before declaring a durable parser/identity fault.
                self._retry(attempt)
        raise AssertionError("unreachable public history retry state")

    def _order_history_since_snapshot(
        self,
        lead_portfolio_id: str,
        *,
        after_update_time_ms: int,
        page_size: int,
        maximum_pages: int,
    ) -> OrderHistoryPage:
        merged: dict[str, PublicLeaderOrder] = {}
        first_total = 0
        covered = False
        for page_number in range(1, maximum_pages + 1):
            page = self._order_history_page(
                lead_portfolio_id,
                page_number=page_number,
                page_size=page_size,
                identity_guard_after_ms=after_update_time_ms,
            )
            if page_number == 1:
                first_total = page.total
                if not page.orders:
                    raise BinancePublicCopyError("COPY_ORDER_HISTORY_WATERMARK_NOT_COVERED")
            for order in page.orders:
                merged.setdefault(order.event_key, order)
            if any(order.update_time_ms <= after_update_time_ms for order in page.orders):
                covered = True
                break
            if len(page.orders) < page_size:
                break
        if not covered:
            raise BinancePublicCopyError("COPY_ORDER_HISTORY_WATERMARK_NOT_COVERED")
        relevant = (
            order for order in merged.values() if order.update_time_ms >= after_update_time_ms
        )
        return OrderHistoryPage(
            orders=tuple(
                sorted(
                    relevant,
                    key=lambda order: (order.update_time_ms, order.event_key),
                )
            ),
            total=first_total,
        )

    def _post(
        self,
        path: str,
        document: Mapping[str, object],
        operation: str,
    ) -> dict[str, Any]:
        body = json.dumps(document, separators=(",", ":")).encode("ascii")
        for attempt in range(self._retry_attempts):
            try:
                result = self._transport(
                    "POST",
                    f"{BINANCE_WEB_BASE}{path}",
                    {
                        "Accept": "application/json",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "Content-Type": "application/json",
                        "User-Agent": "aiq-copy-trading/1.0 (+public-data-poller)",
                    },
                    body,
                )
            except BinancePublicCopyError as error:
                if (
                    str(error) == "COPY_PUBLIC_TRANSPORT_FAILED"
                    and attempt + 1 < self._retry_attempts
                ):
                    self._retry(attempt)
                    continue
                raise
            if result.status in {202, 401, 403}:
                raise BinancePublicCopyError(f"{operation}_ACCESS_DENIED")
            if (result.status == 429 or result.status >= 500) and (
                attempt + 1 < self._retry_attempts
            ):
                self._retry(attempt)
                continue
            try:
                response = json.loads(result.body)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BinancePublicCopyError(f"{operation}_INVALID_JSON") from error
            if not 200 <= result.status < 300:
                raise BinancePublicCopyError(f"{operation}_HTTP_{result.status}")
            if not isinstance(response, dict):
                raise BinancePublicCopyError(f"{operation}_INVALID_RESPONSE")
            if response.get("success") is not True or response.get("code") != "000000":
                if attempt + 1 < self._retry_attempts:
                    self._retry(attempt)
                    continue
                raise BinancePublicCopyError(f"{operation}_API_REJECTED")
            data = response.get("data")
            if not isinstance(data, dict):
                raise BinancePublicCopyError(f"{operation}_INVALID_RESPONSE")
            return data
        raise AssertionError("unreachable public copy retry state")

    def _get_result(
        self,
        path: str,
        operation: str,
    ) -> PublicHttpResult:
        for attempt in range(self._retry_attempts):
            try:
                result = self._transport(
                    "GET",
                    f"{BINANCE_WEB_BASE}{path}",
                    {
                        "Accept": "application/json",
                        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                        "User-Agent": "aiq-copy-trading/1.0 (+public-data-poller)",
                    },
                    b"",
                )
            except BinancePublicCopyError as error:
                if (
                    str(error) == "COPY_PUBLIC_TRANSPORT_FAILED"
                    and attempt + 1 < self._retry_attempts
                ):
                    self._retry(attempt)
                    continue
                raise
            if result.status in {202, 401, 403}:
                raise BinancePublicCopyError(f"{operation}_ACCESS_DENIED")
            if (result.status == 429 or result.status >= 500) and (
                attempt + 1 < self._retry_attempts
            ):
                self._retry(attempt)
                continue
            return result
        raise AssertionError("unreachable public copy retry state")

    def _retry(self, attempt: int) -> None:
        self._sleeper(0.5 * (2**attempt))


def _require_mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PublicCopyDataError("COPY_ROW_INVALID")
    return value


def _leader_with_id(rows: list[object], lead_portfolio_id: str) -> LeaderSnapshot | None:
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("leadPortfolioId")) != lead_portfolio_id:
            continue
        try:
            return LeaderSnapshot.from_api(_require_mapping(row))
        except PublicCopyDataError as error:
            raise BinancePublicCopyError("COPY_LEADER_LOOKUP_INVALID") from error
    return None


def _deduplicate_exact_order_events(
    orders: tuple[PublicLeaderOrder, ...],
) -> tuple[PublicLeaderOrder, ...]:
    """Collapse byte-equivalent normalized rows repeated by the live webpage API."""

    unique: dict[str, PublicLeaderOrder] = {}
    for order in orders:
        unique.setdefault(order.event_key, order)
    return tuple(unique.values())


def _disambiguate_same_timestamp_batch_orders(
    orders: tuple[PublicLeaderOrder, ...],
) -> tuple[PublicLeaderOrder, ...]:
    """Separate provably distinct same-millisecond batch rows without guessing.

    The public endpoint has no order ID. Multiple rows with the same derived
    identity and different update timestamps can be cumulative snapshots of one
    order and remain ambiguous. Distinct-price LIMIT ladder rows already have a
    stable public discriminator. For MARKET orders, only rows that coexist at the
    exact same update timestamp and have distinct average prices prove an atomic
    batch. Every other collision remains rejected.
    """

    grouped: dict[str, list[int]] = defaultdict(list)
    for index, order in enumerate(orders):
        grouped[order.identity_key].append(index)
    resolved = list(orders)
    for indexes in grouped.values():
        if len(indexes) < 2:
            continue
        group = [orders[index] for index in indexes]
        prices = {order.average_price for order in group}
        update_times = {order.update_time_ms for order in group}
        order_types = {order.order_type for order in group}
        prices_are_unique = len(prices) == len(group)
        is_limit_ladder = order_types == {"LIMIT"} and prices_are_unique
        is_atomic_market_batch = (
            order_types == {"MARKET"} and len(update_times) == 1 and prices_are_unique
        )
        if not (is_limit_ladder or is_atomic_market_batch):
            continue
        for index in indexes:
            order = orders[index]
            resolved[index] = order.with_identity_discriminator(
                f"{order.order_type}_BATCH_AVG_PRICE:{order.average_price}"
            )
    return tuple(resolved)


def _position_quantity(
    value: object,
    field: str,
    *,
    allow_zero: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PublicCopyDataError(f"COPY_POSITION_HISTORY_{field.upper()}_INVALID")
    try:
        quantity = Decimal(str(value))
    except InvalidOperation as error:
        raise PublicCopyDataError(f"COPY_POSITION_HISTORY_{field.upper()}_INVALID") from error
    if not quantity.is_finite() or quantity < 0 or (quantity == 0 and not allow_zero):
        raise PublicCopyDataError(f"COPY_POSITION_HISTORY_{field.upper()}_INVALID")
    return quantity
