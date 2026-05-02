import asyncio
from typing import Any, Awaitable, TypeVar

import pytest

from schwab_mcp.approvals import (
    ApprovalDecision,
    ApprovalRequest,
    SignalApprovalManager,
    SignalApprovalSettings,
)
from schwab_mcp.approvals import signal as signal_mod

T = TypeVar("T")


def await_result(awaitable: Awaitable[T]) -> T:
    async def _runner() -> T:
        return await awaitable

    return asyncio.run(_runner())


def _make_manager(
    monkeypatch: pytest.MonkeyPatch, *, timeout_seconds: float = 600.0
) -> tuple[SignalApprovalManager, list[str]]:
    sent: list[str] = []
    counter = {"ts": 1000}

    async def fake_send(self: SignalApprovalManager, body: str) -> int:
        sent.append(body)
        counter["ts"] += 1
        return counter["ts"]

    async def fake_start(self: SignalApprovalManager) -> None:
        return None

    monkeypatch.setattr(SignalApprovalManager, "_send", fake_send)
    monkeypatch.setattr(SignalApprovalManager, "start", fake_start)

    manager = SignalApprovalManager(
        SignalApprovalSettings(
            api_url="http://127.0.0.1:8080",
            account="+15555550100",
            approver_numbers=frozenset({"+15555550199"}),
            timeout_seconds=timeout_seconds,
        )
    )
    return manager, sent


def _request(**overrides: Any) -> ApprovalRequest:
    base: dict[str, Any] = {
        "id": "appr-1",
        "tool_name": "place_equity_order",
        "request_id": "req-1",
        "client_id": None,
        "arguments": {"symbol": '"NVDA"', "quantity": "50"},
    }
    base.update(overrides)
    return ApprovalRequest(**base)


def _reply(
    quoted_ts: int, text: str, *, source: str = "+15555550199"
) -> dict[str, Any]:
    return {
        "envelope": {
            "sourceNumber": source,
            "dataMessage": {"message": text, "quote": {"id": quoted_ts}},
        }
    }


def test_signal_manager_requires_approvers() -> None:
    with pytest.raises(ValueError):
        SignalApprovalManager(
            SignalApprovalSettings(
                api_url="http://127.0.0.1:8080",
                account="+15555550100",
                approver_numbers=frozenset(),
            )
        )


def test_require_approves_on_ok_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = _make_manager(monkeypatch)

    async def scenario() -> ApprovalDecision:
        task = asyncio.create_task(manager.require(_request()))
        await asyncio.sleep(0)
        (sent_ts,) = list(manager._pending)
        await manager._handle_envelope(_reply(sent_ts, "ok"))
        return await task

    decision = await_result(scenario())

    assert decision is ApprovalDecision.APPROVED
    assert "needs approval" in sent[0]
    assert "approved" in sent[-1]


def test_require_approves_on_sync_message_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linked-device mode: the approver's reply arrives as syncMessage.sentMessage."""
    manager, _ = _make_manager(monkeypatch)

    async def scenario() -> ApprovalDecision:
        task = asyncio.create_task(manager.require(_request()))
        await asyncio.sleep(0)
        (sent_ts,) = list(manager._pending)
        await manager._handle_envelope(
            {
                "envelope": {
                    "sourceNumber": "+15555550199",
                    "syncMessage": {
                        "sentMessage": {"message": "ok", "quote": {"id": sent_ts}}
                    },
                }
            }
        )
        return await task

    assert await_result(scenario()) is ApprovalDecision.APPROVED


def test_require_denies_on_no_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = _make_manager(monkeypatch)

    async def scenario() -> ApprovalDecision:
        task = asyncio.create_task(manager.require(_request()))
        await asyncio.sleep(0)
        (sent_ts,) = list(manager._pending)
        await manager._handle_envelope(_reply(sent_ts, "NO"))
        return await task

    assert await_result(scenario()) is ApprovalDecision.DENIED
    assert "denied" in sent[-1]


def test_unauthorized_number_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _ = _make_manager(monkeypatch, timeout_seconds=0.05)

    async def scenario() -> ApprovalDecision:
        task = asyncio.create_task(manager.require(_request()))
        await asyncio.sleep(0)
        (sent_ts,) = list(manager._pending)
        await manager._handle_envelope(_reply(sent_ts, "ok", source="+19998887777"))
        return await task

    assert await_result(scenario()) is ApprovalDecision.EXPIRED


def test_unrecognized_word_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _ = _make_manager(monkeypatch, timeout_seconds=0.05)

    async def scenario() -> ApprovalDecision:
        task = asyncio.create_task(manager.require(_request()))
        await asyncio.sleep(0)
        (sent_ts,) = list(manager._pending)
        await manager._handle_envelope(_reply(sent_ts, "maybe later"))
        return await task

    assert await_result(scenario()) is ApprovalDecision.EXPIRED


def test_reply_without_quote_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _ = _make_manager(monkeypatch, timeout_seconds=0.05)

    async def scenario() -> ApprovalDecision:
        task = asyncio.create_task(manager.require(_request()))
        await asyncio.sleep(0)
        await manager._handle_envelope(
            {
                "envelope": {
                    "sourceNumber": "+15555550199",
                    "dataMessage": {"message": "ok"},
                }
            }
        )
        return await task

    assert await_result(scenario()) is ApprovalDecision.EXPIRED


def test_require_auto_denies_when_body_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, sent = _make_manager(monkeypatch)

    decision = await_result(
        manager.require(
            _request(arguments={"legs": "x" * (signal_mod._BODY_LIMIT + 1)})
        )
    )

    assert decision is ApprovalDecision.DENIED
    assert len(sent) == 1
    assert "auto-denied" in sent[0]
    assert manager._pending == {}


def test_timeout_returns_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, sent = _make_manager(monkeypatch, timeout_seconds=0.01)

    decision = await_result(manager.require(_request()))

    assert decision is ApprovalDecision.EXPIRED
    assert "expired" in sent[-1]
    assert manager._pending == {}


def test_build_body_includes_args_and_instructions() -> None:
    manager = SignalApprovalManager(
        SignalApprovalSettings(
            api_url="http://127.0.0.1:8080",
            account="+15555550100",
            approver_numbers=frozenset({"+15555550199"}),
        )
    )
    body = manager._build_body(
        _request(client_id="client-123"),
        signal_mod.format_arguments({"symbol": '"NVDA"'}),
    )
    assert "place_equity_order" in body
    assert "client-123" in body
    assert 'symbol = "NVDA"' in body
    assert '"ok" to approve' in body


def test_authorized_numbers_normalizes() -> None:
    out = SignalApprovalManager.authorized_numbers([" +15555550199 ", "", "+1555"])
    assert out == frozenset({"+15555550199", "+1555"})
    assert SignalApprovalManager.authorized_numbers(None) == frozenset()
