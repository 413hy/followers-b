"""Exact-destination client for Binance's public copy-trading web data."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ai_quant.copy_trading.models import (
    LeaderSnapshot,
    PublicCopyDataError,
    PublicLeaderOrder,
    SourcePositionSide,
)

BINANCE_WEB_BASE = "https://www.binance.com"
LEADER_LIST_PATH = "/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list"
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


@dataclass(frozen=True, slots=True)
class OrderHistoryPage:
    orders: tuple[PublicLeaderOrder, ...]
    total: int


@dataclass(frozen=True, slots=True)
class ClosedLeaderPosition:
    symbol: str
    position_side: SourcePositionSide
    opened_at_ms: int
    closed_at_ms: int
    maximum_open_quantity: Decimal
    closed_quantity: Decimal


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
    if method != "POST" or url not in {
        f"{BINANCE_WEB_BASE}{LEADER_LIST_PATH}",
        f"{BINANCE_WEB_BASE}{ORDER_HISTORY_PATH}",
        f"{BINANCE_WEB_BASE}{POSITION_HISTORY_PATH}",
    }:
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
        for row in rows:
            try:
                leaders.append(LeaderSnapshot.from_api(_require_mapping(row)))
            except PublicCopyDataError as error:
                if not skip_invalid_rows:
                    raise BinancePublicCopyError(str(error)) from error
                invalid_reasons.append(str(error))
        return LeaderPage(
            leaders=tuple(leaders),
            total=total,
            invalid_row_count=len(invalid_reasons),
            invalid_reason_codes=tuple(sorted(set(invalid_reasons))),
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
        """Read public closed-position intervals used to resolve one-way orders."""

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
                maximum_open_quantity = _position_quantity(
                    row.get("maxOpenInterest"),
                    "maxOpenInterest",
                )
                closed_quantity = _position_quantity(
                    row.get("closedVolume"),
                    "closedVolume",
                )
                if (
                    not isinstance(symbol, str)
                    or not re.fullmatch(r"[A-Z0-9]{3,24}", symbol)
                    or raw_side not in {"Long", "Short"}
                    or not isinstance(opened, int)
                    or isinstance(opened, bool)
                    or not isinstance(closed, int)
                    or isinstance(closed, bool)
                    or opened <= 0
                    or closed < opened
                ):
                    raise PublicCopyDataError("COPY_POSITION_HISTORY_ROW_INVALID")
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


def _position_quantity(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PublicCopyDataError(f"COPY_POSITION_HISTORY_{field.upper()}_INVALID")
    try:
        quantity = Decimal(str(value))
    except InvalidOperation as error:
        raise PublicCopyDataError(f"COPY_POSITION_HISTORY_{field.upper()}_INVALID") from error
    if not quantity.is_finite() or quantity <= 0:
        raise PublicCopyDataError(f"COPY_POSITION_HISTORY_{field.upper()}_INVALID")
    return quantity
