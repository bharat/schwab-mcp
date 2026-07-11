import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

import pytest

from schwab_mcp.approvals import (
    ApprovalDecision,
    ApprovalRequest,
    SignalApprovalManager,
    SignalApprovalSettings,
    signal as signal_mod,
)

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


def _reply(quoted_ts: int, text: str, *, source: str = "+15555550199") -> dict[str, Any]:
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
    assert "Claude Trader" in sent[0]
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
                    "syncMessage": {"sentMessage": {"message": "ok", "quote": {"id": sent_ts}}},
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

    # Use a tool with no friendly renderer so the verbose fallback dumps the
    # arguments verbatim and the body actually overflows.
    decision = await_result(
        manager.require(
            _request(
                tool_name="place_option_combo_order",
                arguments={"legs": "x" * (signal_mod._BODY_LIMIT + 1)},
            )
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


def _settings(**overrides: Any) -> SignalApprovalSettings:
    base: dict[str, Any] = {
        "api_url": "http://127.0.0.1:8080",
        "account": "+15555550100",
        "approver_numbers": frozenset({"+15555550199"}),
    }
    base.update(overrides)
    return SignalApprovalSettings(**base)


def _equity_args(**overrides: Any) -> dict[str, str]:
    """Build a JSON-encoded place_equity_order arguments dict, matching the
    shape produced by `_format_argument` in tools/_registration.py."""
    base: dict[str, Any] = {
        "account_hash": "…5805",
        "symbol": "SCHP",
        "quantity": 1,
        "instruction": "BUY",
        "order_type": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
    }
    base.update(overrides)
    import json as _json

    return {k: _json.dumps(v) for k, v in base.items()}


def test_render_body_friendly_format_for_place_equity_order_with_account_name() -> None:
    manager = SignalApprovalManager(_settings(account_names={"5805": "Rollover IRA"}))
    body = manager._render_body(_request(arguments=_equity_args()))

    assert (
        body == "Claude Trader wants to buy 1 SCHP in the Rollover IRA account. "
        '(Market, Day)\n\nReply "ok" to approve or "no" to deny.'
    )


def test_render_body_falls_back_to_account_last4_when_unmapped() -> None:
    manager = SignalApprovalManager(_settings())
    body = manager._render_body(_request(arguments=_equity_args()))

    assert "in account …5805" in body
    assert "Claude Trader wants to buy" in body


def test_render_body_renders_limit_with_price() -> None:
    manager = SignalApprovalManager(_settings(account_names={"5805": "Rollover IRA"}))
    body = manager._render_body(
        _request(
            arguments=_equity_args(
                instruction="SELL",
                quantity=200,
                symbol="ULTY",
                order_type="LIMIT",
                price=12.34,
                duration="GOOD_TILL_CANCEL",
            )
        )
    )

    assert "sell 200 ULTY" in body
    assert "Limit @ $12.34" in body
    assert "GTC" in body


def test_render_body_renders_stop_limit_with_both_prices() -> None:
    manager = SignalApprovalManager(_settings())
    body = manager._render_body(
        _request(
            arguments=_equity_args(
                order_type="STOP_LIMIT",
                stop_price=25.00,
                price=24.50,
            )
        )
    )

    assert "Stop $25.00 → Limit $24.50" in body


def test_render_body_includes_non_normal_session() -> None:
    manager = SignalApprovalManager(_settings())
    body = manager._render_body(_request(arguments=_equity_args(session="AM")))
    assert "session: Am" in body


def test_render_body_renders_cancel_order() -> None:
    import json as _json

    manager = SignalApprovalManager(_settings(account_names={"5805": "Rollover IRA"}))
    body = manager._render_body(
        _request(
            tool_name="cancel_order",
            arguments={
                "account_hash": _json.dumps("…5805"),
                "order_id": _json.dumps("1006299986057"),
            },
        )
    )

    assert "Claude Trader wants to cancel order 1006299986057 in the Rollover IRA account." in body


def test_render_body_falls_back_to_verbose_for_unknown_tool() -> None:
    manager = SignalApprovalManager(_settings())
    body = manager._render_body(
        _request(
            tool_name="place_option_combo_order",
            arguments={"legs": '["a","b"]'},
        )
    )

    assert "Claude Trader wants to call: place_option_combo_order" in body
    assert "legs = " in body
    assert '"ok" to approve' in body


def test_render_body_recovers_from_renderer_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bug in a per-tool renderer must never block approvals; the verbose
    fallback always runs."""

    def boom(args: Any, account_names: Any) -> str:
        raise RuntimeError("intentional")

    monkeypatch.setitem(signal_mod._TOOL_RENDERERS, "place_equity_order", boom)
    manager = SignalApprovalManager(_settings())
    body = manager._render_body(_request(arguments=_equity_args()))

    assert "Claude Trader wants to call: place_equity_order" in body


def test_parse_account_names_handles_comma_split_and_repeats() -> None:
    parsed = SignalApprovalManager.parse_account_names(
        ["5805=Rollover IRA, 71F7=Roth IRA", "  ", "5805=Rollover IRA v2"]
    )
    assert dict(parsed) == {"5805": "Rollover IRA v2", "71F7": "Roth IRA"}


def test_parse_account_names_skips_malformed_entries() -> None:
    parsed = SignalApprovalManager.parse_account_names(["bogus", "=missing-key", "missing-value=", "5805=OK"])
    assert dict(parsed) == {"5805": "OK"}


def test_authorized_numbers_normalizes() -> None:
    out = SignalApprovalManager.authorized_numbers([" +15555550199 ", "", "+1555"])
    assert out == frozenset({"+15555550199", "+1555"})
    assert SignalApprovalManager.authorized_numbers(None) == frozenset()
