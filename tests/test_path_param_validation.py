"""Tool-boundary validation of path-bound parameters.

schwab-py formats account_hash, order_id, and transaction_id straight into
Schwab API URL paths, so a crafted value can reroute a request to a different
endpoint than the tool name implies (e.g. an account_hash of
'HASH/orders/123#X' turns an account lookup into an order operation, with
httpx dropping everything after '#'). register_tool wraps every tool so such
values fail fast, before approval gating and before schwab-py sees them.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Annotated, Any, cast

import pytest
from mcp.server.mcpserver import Context as MCPContext, MCPServer
from mcp.server.mcpserver.tools import Tool

from schwab_mcp.approvals import ApprovalDecision, ApprovalManager, ApprovalRequest
from schwab_mcp.context import SchwabContext, SchwabServerContext
from schwab_mcp.tools._registration import register_tool

CRAFTED_HASH = "0123456789ABCDEF9999/orders/999000111#5842"


class RecordingApprovalManager(ApprovalManager):
    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision.APPROVED


def _registered(
    func: Any, *, write: bool = False
) -> tuple[Tool, MCPContext, list[dict[str, Any]], RecordingApprovalManager]:
    server = MCPServer(name="path-param-validation")
    calls: list[dict[str, Any]] = []
    approvals = RecordingApprovalManager()
    lifespan_context = SchwabServerContext(
        client=cast(Any, SimpleNamespace(calls=calls)),
        approval_manager=approvals,
    )
    request_context = SimpleNamespace(
        lifespan_context=lifespan_context,
        request_id="request-123",
        meta=None,
    )
    context = MCPContext.model_construct(
        _request_context=cast(Any, request_context),
        _mcp_server=server,
    )
    register_tool(server, func, write=write)
    tool_manager = server._tool_manager
    tool = next(t for t in cast(list[Tool], tool_manager.list_tools()) if t.name == func.__name__)
    return tool, context, calls, approvals


def _make_lookup_tool(calls_sink: list[dict[str, Any]] | None = None):
    recorded: list[dict[str, Any]] = calls_sink if calls_sink is not None else []

    async def lookup_order(
        ctx: SchwabContext,
        account_hash: Annotated[str, "Account hash"],
        order_id: Annotated[str, "Order ID"],
    ) -> dict[str, Any]:
        """Fake read tool standing in for get_order."""
        recorded.append({"account_hash": account_hash, "order_id": order_id})
        return {"ok": True}

    return lookup_order, recorded


def test_valid_values_pass_through() -> None:
    func, recorded = _make_lookup_tool()
    tool, context, _, _ = _registered(func)

    result = asyncio.run(tool.fn(context, "0123456789ABCDEF5842", "1006299986987"))

    assert result == {"ok": True}
    assert recorded == [{"account_hash": "0123456789ABCDEF5842", "order_id": "1006299986987"}]


@pytest.mark.parametrize(
    "bad_hash",
    [
        CRAFTED_HASH,
        "HASH/../other",
        "HASH?x=1",
        "HASH#frag",
        "HASH%2Forders",
        "…5842",
        "hash with space",
        "",
    ],
)
def test_crafted_account_hash_is_rejected_on_read_tool(bad_hash: str) -> None:
    """Read tools reroute too (the preview tools are registered read-only),
    so validation must not depend on the approval wrapper."""
    func, recorded = _make_lookup_tool()
    tool, context, _, _ = _registered(func)

    with pytest.raises(ValueError, match="account_hash"):
        asyncio.run(tool.fn(context, bad_hash, "1006299986987"))

    assert recorded == []


@pytest.mark.parametrize("bad_order_id", ["999000111#5842", "123\u202e456", "DROP TABLE", "12.3", ""])
def test_non_digit_order_id_is_rejected(bad_order_id: str) -> None:
    func, recorded = _make_lookup_tool()
    tool, context, _, _ = _registered(func)

    with pytest.raises(ValueError, match="order_id"):
        asyncio.run(tool.fn(context, "0123456789ABCDEF5842", bad_order_id))

    assert recorded == []


def test_crafted_hash_on_write_tool_fails_before_approval() -> None:
    """An invalid value must never reach the reviewer: the error fires before
    the approval request is created, so the prompt cannot misrepresent it."""
    recorded: list[dict[str, Any]] = []

    async def cancel_order(
        ctx: SchwabContext,
        account_hash: Annotated[str, "Account hash"],
        order_id: Annotated[str, "Order ID"],
    ) -> dict[str, Any]:
        """Fake write tool standing in for the real cancel_order."""
        recorded.append({"account_hash": account_hash, "order_id": order_id})
        return {"ok": True}

    tool, context, _, approvals = _registered(cancel_order, write=True)

    with pytest.raises(ValueError, match="account_hash"):
        asyncio.run(tool.fn(context, CRAFTED_HASH, "1006299986987"))

    assert approvals.requests == []
    assert recorded == []


def test_none_is_allowed_for_optional_account_hash() -> None:
    """get_account-style tools default account_hash to None; the guard must
    not break the default-account fallback."""

    async def whoami(
        ctx: SchwabContext,
        account_hash: Annotated[str | None, "Account hash"] = None,
    ) -> dict[str, Any]:
        """Fake tool with an optional account hash."""
        return {"account_hash": account_hash}

    tool, context, _, _ = _registered(whoami)

    assert asyncio.run(tool.fn(context)) == {"account_hash": None}


def test_transaction_id_is_guarded() -> None:
    async def lookup_transaction(
        ctx: SchwabContext,
        account_hash: Annotated[str, "Account hash"],
        transaction_id: Annotated[str, "Transaction ID"],
    ) -> dict[str, Any]:
        """Fake tool standing in for get_transaction."""
        return {"ok": True}

    tool, context, _, _ = _registered(lookup_transaction)

    with pytest.raises(ValueError, match="transaction_id"):
        asyncio.run(tool.fn(context, "0123456789ABCDEF5842", "123/456"))
