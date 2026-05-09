from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import httpx
import websockets

from schwab_mcp.approvals.base import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
    format_arguments,
)

logger = logging.getLogger(__name__)

# Signal's per-message text limit is ~2000 chars; leave headroom for the
# header/footer so the rendered arguments are never partially shown.
_BODY_LIMIT = 1800

_APPROVE_WORDS = frozenset({"ok", "yes", "y", "approve", "approved", "✅", "👍"})
_DENY_WORDS = frozenset({"no", "n", "deny", "denied", "❌", "👎"})

_AGENT_NAME = "Claude Trader"
_FOOTER = 'Reply "ok" to approve or "no" to deny.'

_EMPTY_NAMES: Mapping[str, str] = MappingProxyType({})


@dataclass(slots=True, frozen=True)
class SignalApprovalSettings:
    """Configuration values required for Signal approvals."""

    api_url: str
    account: str
    approver_numbers: frozenset[str]
    timeout_seconds: float = 600.0
    account_names: Mapping[str, str] = field(default_factory=lambda: _EMPTY_NAMES)
    """Map of last-4-chars of account_hash to a friendly name (e.g. ``5805 ->
    "Rollover IRA"``). Used by the per-tool message renderers to refer to
    accounts by name instead of by redacted-hash suffix. Empty map preserves
    the previous behaviour (always show ``…XXXX``)."""


@dataclass(slots=True)
class _PendingApproval:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalDecision]
    sent_timestamp: int


class SignalApprovalManager(ApprovalManager):
    """Approval manager that routes decisions through Signal replies.

    Talks to a local signal-cli REST daemon (bbernhard/signal-cli-rest-api or
    ``signal-cli daemon --http``) so no public endpoint is exposed. Correlation
    uses Signal's native reply-to: the daemon returns the sent message's
    timestamp, and an incoming reply carries that timestamp in ``quote.id``.
    """

    def __init__(self, settings: SignalApprovalSettings) -> None:
        if not settings.approver_numbers:
            raise ValueError(
                "SignalApprovalManager requires at least one approver number."
            )
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=settings.api_url, timeout=None)
        self._receiver: asyncio.Task[None] | None = None
        self._pending: dict[int, _PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._receiver is not None:
            return
        loop = asyncio.get_running_loop()
        self._receiver = loop.create_task(self._receive_loop())

    async def stop(self) -> None:
        if self._receiver is not None:
            self._receiver.cancel()
            try:
                await self._receiver
            except asyncio.CancelledError:
                pass
            self._receiver = None
        await self._client.aclose()

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        await self.start()

        body = self._render_body(request)
        if len(body) > _BODY_LIMIT:
            logger.warning(
                "Auto-denying approval %s for tool '%s': body too large to "
                "display in full (%d chars)",
                request.id,
                request.tool_name,
                len(body),
            )
            await self._send_best_effort(
                f"❌ schwab-mcp auto-denied '{request.tool_name}' "
                f"(approval {request.id}): arguments too large to display in "
                f"full ({len(body)} chars). Approving a partial view is unsafe."
            )
            return ApprovalDecision.DENIED

        sent_timestamp = await self._send(body)
        future: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        pending = _PendingApproval(
            request=request, future=future, sent_timestamp=sent_timestamp
        )
        async with self._lock:
            self._pending[sent_timestamp] = pending

        try:
            decision = await asyncio.wait_for(
                future, timeout=self._settings.timeout_seconds
            )
        except asyncio.TimeoutError:
            decision = ApprovalDecision.EXPIRED
            await self._send_best_effort(
                f"⏱️ schwab-mcp approval {request.id} for "
                f"'{request.tool_name}' expired after "
                f"{int(self._settings.timeout_seconds)}s."
            )
        finally:
            async with self._lock:
                self._pending.pop(sent_timestamp, None)

        return decision

    async def _send(self, body: str) -> int:
        response = await self._client.post(
            "/v2/send",
            json={
                "number": self._settings.account,
                "recipients": sorted(self._settings.approver_numbers),
                "message": body,
            },
        )
        response.raise_for_status()
        return int(response.json()["timestamp"])

    async def _send_best_effort(self, body: str) -> None:
        try:
            await self._send(body)
        except httpx.HTTPError:
            logger.exception("Failed to post Signal notice")

    async def _receive_loop(self) -> None:
        ws_url = (
            self._settings.api_url.replace("http", "ws", 1)
            + f"/v1/receive/{self._settings.account}"
        )
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    async for frame in ws:
                        await self._handle_envelope(json.loads(frame))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Signal receive websocket error; reconnecting in 5s")
                await asyncio.sleep(5)

    async def _handle_envelope(self, envelope: dict[str, Any]) -> None:
        env = envelope.get("envelope", envelope)
        source = env.get("sourceNumber") or env.get("source")
        # In linked-device mode the approver's reply is their own outgoing
        # message, delivered to us as syncMessage.sentMessage rather than an
        # incoming dataMessage. Accept either shape.
        sync = (env.get("syncMessage") or {}).get("sentMessage")
        data = env.get("dataMessage") or sync or {}
        quote = data.get("quote") or {}
        quoted_ts = quote.get("id")
        text = (data.get("message") or "").strip().lower()

        if quoted_ts is None or not text:
            return
        if source not in self._settings.approver_numbers:
            logger.debug("Ignoring Signal reply from unauthorized number %s", source)
            return

        async with self._lock:
            pending = self._pending.get(int(quoted_ts))
        if pending is None or pending.future.done():
            return

        if text in _APPROVE_WORDS:
            decision = ApprovalDecision.APPROVED
        elif text in _DENY_WORDS:
            decision = ApprovalDecision.DENIED
        else:
            return

        pending.future.set_result(decision)
        marker = "✅" if decision is ApprovalDecision.APPROVED else "❌"
        await self._send_best_effort(
            f"{marker} schwab-mcp approval {pending.request.id} for "
            f"'{pending.request.tool_name}' {decision.value} by {source}."
        )

    def _render_body(self, request: ApprovalRequest) -> str:
        """Render the Signal message body for an approval request.

        Per-tool renderers produce a one- or two-line natural-language summary
        for the common write tools. Anything we don't have a custom renderer
        for falls back to a slimmed-down YAML-style argument dump.
        """
        renderer = _TOOL_RENDERERS.get(request.tool_name)
        if renderer is not None:
            try:
                args = _decode_arguments(request.arguments)
                summary = renderer(args, self._settings.account_names)
            except Exception:
                # Never fail the approval just because the friendly renderer
                # had a bug — fall through to the verbose format so the user
                # still sees the raw arguments and can decide.
                logger.exception(
                    "Friendly renderer failed for tool '%s' (approval %s); "
                    "falling back to verbose format.",
                    request.tool_name,
                    request.id,
                )
            else:
                return f"{summary}\n\n{_FOOTER}"

        rendered_args = format_arguments(request.arguments)
        return (
            f"{_AGENT_NAME} wants to call: {request.tool_name}\n"
            f"\n"
            f"{rendered_args}\n"
            f"\n"
            f"{_FOOTER}"
        )

    @staticmethod
    def authorized_numbers(values: Sequence[str] | None) -> frozenset[str]:
        """Normalize a sequence of approver phone numbers."""
        if not values:
            return frozenset()
        return frozenset(v.strip() for v in values if v.strip())

    @staticmethod
    def parse_account_names(values: Sequence[str] | None) -> Mapping[str, str]:
        """Parse ``last4=Name`` entries (from CLI flags or comma-split env) into
        an immutable mapping. Repeated keys: last value wins. Whitespace and
        empty entries are ignored. Invalid entries are skipped with a warning.
        """
        if not values:
            return _EMPTY_NAMES
        result: dict[str, str] = {}
        for raw in values:
            if not raw:
                continue
            for chunk in raw.split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if "=" not in chunk:
                    logger.warning(
                        "Ignoring malformed account-name entry %r (expected "
                        "'last4=Name')",
                        chunk,
                    )
                    continue
                last4, _, name = chunk.partition("=")
                last4 = last4.strip()
                name = name.strip()
                if not last4 or not name:
                    logger.warning("Ignoring empty account-name entry %r", chunk)
                    continue
                result[last4] = name
        return MappingProxyType(result) if result else _EMPTY_NAMES


# --------------------------------------------------------------------------- #
# Per-tool message renderers
# --------------------------------------------------------------------------- #


def _decode_arguments(arguments: Mapping[str, str]) -> dict[str, Any]:
    """Decode the JSON-encoded argument values produced by `_format_argument`
    in `tools/_registration.py`. Falls back to the raw string on a decode
    failure so a single bad value never sinks the whole renderer."""
    decoded: dict[str, Any] = {}
    for name, raw in arguments.items():
        try:
            decoded[name] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            decoded[name] = raw
    return decoded


def _account_label(account_value: Any, account_names: Mapping[str, str]) -> str:
    """Resolve the account display label from a (redacted) account_hash value.

    The wrapper in `_registration.py` redacts `account_hash` to `…XXXX` (last
    4 chars). We strip the leading horizontal-ellipsis if present and look up
    the friendly name; otherwise fall back to ``account …XXXX``.
    """
    if not isinstance(account_value, str):
        return "the requested account"
    last4 = account_value.lstrip("…").strip()
    friendly = account_names.get(last4)
    if friendly:
        return f"the {friendly} account"
    if last4:
        return f"account …{last4}"
    return "the requested account"


def _format_money(value: Any) -> str | None:
    """Render a numeric price as a dollar string, or None if not numeric."""
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return f"${amount:,.2f}"


_DURATION_LABELS = {
    "DAY": "Day",
    "GOOD_TILL_CANCEL": "GTC",
    "FILL_OR_KILL": "FOK",
    "IMMEDIATE_OR_CANCEL": "IOC",
    "END_OF_WEEK": "End of week",
    "END_OF_MONTH": "End of month",
    "NEXT_END_OF_MONTH": "Next end of month",
}


def _duration_label(value: Any) -> str:
    if not isinstance(value, str):
        return "Day"
    return _DURATION_LABELS.get(value.upper(), value.title().replace("_", " "))


def _order_descriptor(args: Mapping[str, Any]) -> str:
    """Render the ``(Type, Duration[, session])`` descriptor used at the end
    of an order message."""
    order_type = str(args.get("order_type") or "MARKET").upper()
    parts: list[str] = []
    if order_type == "MARKET":
        parts.append("Market")
    elif order_type == "LIMIT":
        price = _format_money(args.get("price"))
        parts.append(f"Limit @ {price}" if price else "Limit")
    elif order_type == "STOP":
        stop = _format_money(args.get("stop_price"))
        parts.append(f"Stop @ {stop}" if stop else "Stop")
    elif order_type == "STOP_LIMIT":
        stop = _format_money(args.get("stop_price"))
        limit = _format_money(args.get("price"))
        if stop and limit:
            parts.append(f"Stop {stop} → Limit {limit}")
        elif stop:
            parts.append(f"Stop-Limit (stop {stop})")
        else:
            parts.append("Stop-Limit")
    elif order_type == "TRAILING_STOP":
        parts.append("Trailing Stop")
    else:
        parts.append(order_type.title().replace("_", " "))

    parts.append(_duration_label(args.get("duration")))

    session = args.get("session")
    if isinstance(session, str) and session.upper() not in {"NORMAL", ""}:
        parts.append(f"session: {session.title()}")

    return f"({', '.join(parts)})"


def _render_place_equity_order(
    args: Mapping[str, Any], account_names: Mapping[str, str]
) -> str:
    instruction = str(args.get("instruction") or "").lower() or "trade"
    qty = args.get("quantity") or "?"
    symbol = str(args.get("symbol") or "?")
    account = _account_label(args.get("account_hash"), account_names)
    descriptor = _order_descriptor(args)
    return (
        f"{_AGENT_NAME} wants to {instruction} {qty} {symbol} in "
        f"{account}. {descriptor}"
    )


def _render_cancel_order(
    args: Mapping[str, Any], account_names: Mapping[str, str]
) -> str:
    order_id = args.get("order_id") or "?"
    account = _account_label(args.get("account_hash"), account_names)
    return f"{_AGENT_NAME} wants to cancel order {order_id} in {account}."


_TOOL_RENDERERS: Mapping[str, Any] = {
    "place_equity_order": _render_place_equity_order,
    "cancel_order": _render_cancel_order,
}


__all__ = ["SignalApprovalManager", "SignalApprovalSettings"]
