import asyncio
import datetime
import json
from types import SimpleNamespace
from typing import Any, Awaitable, TypeVar, cast

import pytest
from schwab.client import AsyncClient

from mcp.server.fastmcp import Context as MCPContext

from schwab_mcp.approvals import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
    DiscordApprovalManager,
    DiscordApprovalSettings,
    NoOpApprovalManager,
)
from schwab_mcp.context import SchwabContext, SchwabServerContext
from schwab_mcp.tools import _registration


class RecordingApprovalManager(ApprovalManager):
    def __init__(self, decision: ApprovalDecision) -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self.decision


class DummySession:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.progress: list[dict[str, Any]] = []

    async def send_log_message(self, **payload: Any) -> None:
        self.messages.append(payload)

    async def send_progress_notification(
        self,
        *,
        progress_token: str,
        progress: float,
        total: float | None,
        message: str | None,
    ) -> None:
        self.progress.append(
            {
                "progress_token": progress_token,
                "progress": progress,
                "total": total,
                "message": message,
            }
        )


def make_ctx(
    decision: ApprovalDecision,
    *,
    progress_token: str | None = None,
) -> tuple[SchwabContext, RecordingApprovalManager, DummySession, Any]:
    approval_manager = RecordingApprovalManager(decision)
    lifespan_context = SchwabServerContext(
        client=cast(AsyncClient, object()),
        approval_manager=approval_manager,
    )
    session = DummySession()
    meta = (
        SimpleNamespace(progressToken=progress_token, client_id="client-123")
        if progress_token
        else None
    )
    request_context = SimpleNamespace(
        lifespan_context=lifespan_context,
        request_id="req-123",
        session=session,
        meta=meta,
    )
    ctx = SchwabContext.model_construct(
        _request_context=cast(Any, request_context),
        _fastmcp=None,
    )
    return ctx, approval_manager, session, request_context


async def sample_write_tool(ctx: SchwabContext, symbol: str) -> str:
    return symbol.upper()


def wrapped_tool():
    ensured = _registration._ensure_schwab_context(sample_write_tool)
    return _registration._wrap_with_approval(ensured)


T = TypeVar("T")


def await_result(awaitable: Awaitable[T]) -> T:
    async def _runner() -> T:
        return await awaitable

    return asyncio.run(_runner())


def test_write_tool_runs_when_approved() -> None:
    ctx, approval_manager, session, _ = make_ctx(ApprovalDecision.APPROVED)
    tool = wrapped_tool()

    result = await_result(tool(ctx, "spy"))

    assert result == "SPY"
    assert len(approval_manager.requests) == 1
    request = approval_manager.requests[0]
    assert request.tool_name == "sample_write_tool"
    assert request.arguments["symbol"] == '"spy"'
    assert session.messages == []


def test_write_tool_denied_raises_permission_error() -> None:
    ctx, approval_manager, session, _ = make_ctx(ApprovalDecision.DENIED)
    tool = wrapped_tool()

    with pytest.raises(PermissionError):
        await_result(tool(ctx, "spy"))

    assert len(approval_manager.requests) == 1
    assert len(session.messages) == 1
    assert session.messages[0]["level"] == "warning"


def test_write_tool_timeout_raises_timeout_error() -> None:
    ctx, approval_manager, session, _ = make_ctx(ApprovalDecision.EXPIRED)
    tool = wrapped_tool()

    with pytest.raises(TimeoutError):
        await_result(tool(ctx, "spy"))

    assert len(approval_manager.requests) == 1
    assert len(session.messages) == 1
    assert session.messages[0]["level"] == "warning"


def test_write_tool_accepts_base_context() -> None:
    _, approval_manager, session, request_context = make_ctx(ApprovalDecision.APPROVED)
    base_ctx = MCPContext.model_construct(
        _request_context=cast(Any, request_context),
        _fastmcp=None,
    )
    tool = wrapped_tool()

    result = await_result(tool(base_ctx, "spy"))

    assert result == "SPY"
    assert len(approval_manager.requests) == 1
    assert session.messages == []


def test_progress_notifications_emitted_when_supported() -> None:
    ctx, approval_manager, session, _ = make_ctx(
        ApprovalDecision.APPROVED, progress_token="token-1"
    )
    tool = wrapped_tool()

    result = await_result(tool(ctx, "spy"))

    assert result == "SPY"
    assert [entry["progress"] for entry in session.progress] == [0, 1]
    assert session.progress[0]["message"].startswith("Waiting for reviewer approval")
    assert session.progress[1]["message"].startswith("Reviewer approved")


def test_noop_manager_logs_every_auto_approval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = NoOpApprovalManager()
    request = ApprovalRequest(
        id="appr-1",
        tool_name="place_equity_order",
        request_id="req-1",
        client_id=None,
        arguments={"symbol": '"NVDA"', "account_hash": '"\\u2026WXYZ"'},
    )

    with caplog.at_level("WARNING", logger="schwab_mcp.approvals.base"):
        decision = await_result(manager.require(request))

    assert decision is ApprovalDecision.APPROVED
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert "place_equity_order" in record.getMessage()
    assert "req-1" in record.getMessage()


def test_discord_manager_requires_approvers() -> None:
    settings = DiscordApprovalSettings(
        token="token",
        channel_id=123,
        approver_ids=frozenset(),
    )
    with pytest.raises(ValueError):
        DiscordApprovalManager(settings)


def test_format_argument_returns_full_json_without_truncation() -> None:
    big = {"legs": [{"sym": f"SPY {i}", "qty": i} for i in range(50)]}
    out = _registration._format_argument(big)
    assert out == json.dumps(big)
    assert len(out) > 256
    assert not out.endswith("...")


def test_format_argument_handles_non_json_values() -> None:
    out = _registration._format_argument({"d": datetime.date(2024, 1, 2)})
    assert "2024" in out


def test_redact_masks_account_hash_to_last_four() -> None:
    assert _registration._redact("account_hash", "ABCDEFGHIJ") == "…GHIJ"
    assert _registration._redact("account_hash", "abc") == "…"
    assert _registration._redact("symbol", "ABCDEFGHIJ") == "ABCDEFGHIJ"


async def sample_order_tool(ctx: SchwabContext, account_hash: str, symbol: str) -> str:
    return f"{account_hash}:{symbol}"


def test_account_hash_redacted_in_approval_request() -> None:
    ctx, approval_manager, _, _ = make_ctx(ApprovalDecision.APPROVED)
    ensured = _registration._ensure_schwab_context(sample_order_tool)
    tool = _registration._wrap_with_approval(ensured)

    result = await_result(tool(ctx, "ABCD1234WXYZ", "AAPL"))

    assert result == "ABCD1234WXYZ:AAPL"
    request = approval_manager.requests[0]
    assert request.arguments["account_hash"] == '"\\u2026WXYZ"'
    assert request.arguments["symbol"] == '"AAPL"'


def test_discord_format_arguments_wraps_in_code_fence() -> None:
    out = DiscordApprovalManager._format_arguments({"symbol": '"AAPL"', "qty": "10"})
    assert out.startswith("```\n")
    assert out.endswith("\n```")
    assert 'symbol = "AAPL"' in out
    assert "qty = 10" in out


def test_discord_format_arguments_sanitizes_backticks() -> None:
    out = DiscordApprovalManager._format_arguments(
        {"symbol": '"AAPL ``` [evil](https://x)"'}
    )
    assert out.count("```") == 2
    assert "`" not in out.removeprefix("```").removesuffix("```")


def test_discord_format_arguments_never_truncates() -> None:
    big = {"spec": "x" * 2000}
    out = DiscordApprovalManager._format_arguments(big)
    assert "x" * 2000 in out
    assert not out.endswith("...")


def _make_discord_manager() -> DiscordApprovalManager:
    return DiscordApprovalManager(
        DiscordApprovalSettings(
            token="token",
            channel_id=123,
            approver_ids=frozenset({1}),
        )
    )


def test_discord_finalize_message_omits_arguments() -> None:
    manager = _make_discord_manager()

    captured: dict[str, Any] = {}

    class FakeMessage:
        async def edit(self, *, embed: Any) -> None:
            captured["embed"] = embed

    request = ApprovalRequest(
        id="approval-1",
        tool_name="place_equity_order",
        request_id="req-1",
        client_id=None,
        arguments={"account_hash": '"…WXYZ"', "symbol": '"AAPL"'},
    )

    await_result(
        manager._finalize_message(
            cast(Any, FakeMessage()),
            request,
            ApprovalDecision.APPROVED,
            actor=None,
            reason=None,
        )
    )

    embed = captured["embed"]
    field_names = [f.name for f in embed.fields]
    assert "Arguments" not in field_names


def test_discord_require_auto_denies_when_arguments_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _make_discord_manager()

    sent: list[Any] = []

    class FakeMessage:
        id = 999

        async def add_reaction(self, _: str) -> None:
            raise AssertionError("reactions must not be added on auto-deny path")

    class FakeChannel:
        async def send(self, *, embed: Any) -> Any:
            sent.append(embed)
            return FakeMessage()

    async def fake_start(self: DiscordApprovalManager) -> None:
        return None

    async def fake_ensure_channel(self: DiscordApprovalManager) -> Any:
        return FakeChannel()

    monkeypatch.setattr(DiscordApprovalManager, "start", fake_start)
    monkeypatch.setattr(DiscordApprovalManager, "_ensure_channel", fake_ensure_channel)

    request = ApprovalRequest(
        id="approval-2",
        tool_name="place_option_combo_order",
        request_id="req-2",
        client_id=None,
        arguments={"legs": "x" * 1200},
    )

    decision = await_result(manager.require(request))

    assert decision is ApprovalDecision.DENIED
    assert len(sent) == 1
    assert "auto-denied" in sent[0].title.lower()
