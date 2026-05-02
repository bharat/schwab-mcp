from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

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


@dataclass(slots=True, frozen=True)
class SignalApprovalSettings:
    """Configuration values required for Signal approvals."""

    api_url: str
    account: str
    approver_numbers: frozenset[str]
    timeout_seconds: float = 600.0


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

        rendered_args = format_arguments(request.arguments)
        body = self._build_body(request, rendered_args)
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
        while True:
            try:
                response = await self._client.get(
                    f"/v1/receive/{self._settings.account}",
                    params={"timeout": 30},
                )
                response.raise_for_status()
                for envelope in response.json():
                    await self._handle_envelope(envelope)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Signal receive loop error; retrying in 5s")
                await asyncio.sleep(5)

    async def _handle_envelope(self, envelope: dict[str, Any]) -> None:
        env = envelope.get("envelope", envelope)
        source = env.get("sourceNumber") or env.get("source")
        data = env.get("dataMessage") or {}
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

    def _build_body(self, request: ApprovalRequest, rendered_args: str) -> str:
        lines = [
            "⚠️ schwab-mcp: write operation needs approval",
            "",
            f"tool        {request.tool_name}",
            f"approval    {request.id}",
            f"request     {request.request_id}",
        ]
        if request.client_id:
            lines.append(f"client      {request.client_id}")
        lines += [
            "",
            rendered_args,
            "",
            'Reply to this message with "ok" to approve or "no" to deny.',
            f"Expires in {int(self._settings.timeout_seconds)}s.",
        ]
        return "\n".join(lines)

    @staticmethod
    def authorized_numbers(values: Sequence[str] | None) -> frozenset[str]:
        """Normalize a sequence of approver phone numbers."""
        if not values:
            return frozenset()
        return frozenset(v.strip() for v in values if v.strip())


__all__ = ["SignalApprovalManager", "SignalApprovalSettings"]
