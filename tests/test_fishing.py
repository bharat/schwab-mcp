from __future__ import annotations

import asyncio
import random
from typing import Any, cast

import pytest
from conftest import make_ctx, run

from schwab_mcp.tools import fishing
from schwab_mcp.tools.fishing import (
    PATTERN_LINEAR,
    PATTERN_RANDOM_WALK,
    PATTERN_STAGGERED,
    STATUS_CANCELED,
    STATUS_FILLED,
    STATUS_RUNNING,
    _auto_chunks,
    _Campaign,
    _initial_prices,
    _jittered_interval,
    _next_price,
    _select_subs_to_adjust,
    _SubOrder,
    _validate_inputs,
)

# ===== Input validation =====


class TestValidateInputs:
    def _ok_args(self, **overrides) -> dict[str, Any]:
        base = dict(
            instruction="SELL_TO_OPEN",
            quantity=8,
            range_start=9.0,
            range_end=8.4,
            step=0.05,
            pattern="random_walk",
            chunks=None,
            step_interval_seconds=300.0,
            timing_jitter_pct=0.4,
        )
        base.update(overrides)
        return base

    def test_basic_sell_passes(self):
        instr, chunks, pattern = _validate_inputs(**self._ok_args())
        assert instr == "SELL_TO_OPEN"
        assert sum(chunks) == 8
        assert pattern == "random_walk"

    def test_basic_buy_passes(self):
        instr, chunks, pattern = _validate_inputs(
            **self._ok_args(instruction="BUY_TO_CLOSE", range_start=8.4, range_end=9.0)
        )
        assert instr == "BUY_TO_CLOSE"
        assert sum(chunks) == 8

    def test_invalid_instruction(self):
        with pytest.raises(ValueError, match="instruction must be"):
            _validate_inputs(**self._ok_args(instruction="HODL"))

    def test_sell_wrong_direction_rejected(self):
        with pytest.raises(ValueError, match="SELL: range_start"):
            _validate_inputs(**self._ok_args(range_start=8.4, range_end=9.0))

    def test_buy_wrong_direction_rejected(self):
        with pytest.raises(ValueError, match="BUY: range_start"):
            _validate_inputs(**self._ok_args(instruction="BUY_TO_OPEN", range_start=9.0, range_end=8.4))

    def test_zero_quantity_rejected(self):
        with pytest.raises(ValueError, match="quantity must be > 0"):
            _validate_inputs(**self._ok_args(quantity=0))

    def test_zero_step_rejected(self):
        with pytest.raises(ValueError, match="step must be > 0"):
            _validate_inputs(**self._ok_args(step=0))

    def test_step_larger_than_range_rejected(self):
        with pytest.raises(ValueError, match="range span"):
            _validate_inputs(**self._ok_args(step=1.0))

    def test_unknown_pattern_rejected(self):
        with pytest.raises(ValueError, match="pattern must be"):
            _validate_inputs(**self._ok_args(pattern="lightning"))

    def test_low_interval_rejected(self):
        with pytest.raises(ValueError, match="step_interval_seconds"):
            _validate_inputs(**self._ok_args(step_interval_seconds=10))

    def test_jitter_out_of_range(self):
        with pytest.raises(ValueError, match="timing_jitter_pct"):
            _validate_inputs(**self._ok_args(timing_jitter_pct=1.5))

    def test_chunks_sum_mismatch_rejected(self):
        with pytest.raises(ValueError, match="sum.chunks.=7"):
            _validate_inputs(**self._ok_args(chunks=[3, 2, 2]))

    def test_chunks_with_zero_rejected(self):
        with pytest.raises(ValueError, match="each chunk must be"):
            _validate_inputs(**self._ok_args(chunks=[5, 0, 3]))

    def test_chunks_explicit_passes(self):
        _, chunks, _ = _validate_inputs(**self._ok_args(chunks=[3, 2, 2, 1]))
        assert chunks == [3, 2, 2, 1]


class TestAutoChunks:
    def test_small_quantity(self):
        assert _auto_chunks(1) == [1]
        assert _auto_chunks(2) == [1, 1]

    def test_medium_quantity(self):
        chunks = _auto_chunks(8)
        assert sum(chunks) == 8
        assert 2 <= len(chunks) <= 4

    def test_large_quantity(self):
        chunks = _auto_chunks(50)
        assert sum(chunks) == 50
        assert 2 <= len(chunks) <= 4


# ===== Pattern logic =====


def _make_campaign(
    *,
    instruction: str = "SELL_TO_OPEN",
    range_start: float = 9.0,
    range_end: float = 8.4,
    step: float = 0.05,
    pattern: str = PATTERN_LINEAR,
    chunks: list[int] | None = None,
    timing_jitter_pct: float = 0.0,
    rng_seed: int = 42,
) -> _Campaign:
    chunks = chunks or [3, 2, 2, 1]
    return _Campaign(
        id="test-campaign",
        account_hash="0123456789ABCDEFsuffix1234",
        symbol="COIN  260717C00250000",
        instruction=instruction,
        quantity=sum(chunks),
        chunks=chunks,
        range_start=range_start,
        range_end=range_end,
        step=step,
        pattern=pattern,
        step_interval_seconds=300.0,
        timing_jitter_pct=timing_jitter_pct,
        session="NORMAL",
        duration="DAY",
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        status=STATUS_RUNNING,
        rng=random.Random(rng_seed),  # noqa: S311 - deterministic test seeding, not crypto
    )


class TestInitialPrices:
    def test_linear_all_same(self):
        c = _make_campaign(pattern=PATTERN_LINEAR)
        prices = _initial_prices(c)
        assert prices == [9.0, 9.0, 9.0, 9.0]

    def test_staggered_descends_for_sell(self):
        c = _make_campaign(pattern=PATTERN_STAGGERED)
        prices = _initial_prices(c)
        # Each later chunk should be ≤ earlier chunk for SELL
        for i in range(len(prices) - 1):
            assert prices[i] >= prices[i + 1]
        # First chunk at range_start
        assert prices[0] == pytest.approx(9.0)
        # Stays within range
        for p in prices:
            assert c.range_end <= p <= c.range_start

    def test_staggered_ascends_for_buy(self):
        c = _make_campaign(
            instruction="BUY_TO_CLOSE",
            range_start=8.4,
            range_end=9.0,
            pattern=PATTERN_STAGGERED,
        )
        prices = _initial_prices(c)
        for i in range(len(prices) - 1):
            assert prices[i] <= prices[i + 1]
        assert prices[0] == pytest.approx(8.4)
        for p in prices:
            assert 8.4 <= p <= 9.0

    def test_random_walk_within_range(self):
        c = _make_campaign(pattern=PATTERN_RANDOM_WALK, rng_seed=123)
        prices = _initial_prices(c)
        for p in prices:
            assert c.range_end <= p <= c.range_start


class TestNextPrice:
    def test_linear_sell_steps_down(self):
        c = _make_campaign(pattern=PATTERN_LINEAR)
        sub = _SubOrder(chunk_idx=0, quantity=1, price=9.0)
        nxt = _next_price(c, sub)
        assert nxt == pytest.approx(8.95)

    def test_linear_buy_steps_up(self):
        c = _make_campaign(
            instruction="BUY_TO_OPEN",
            range_start=8.4,
            range_end=9.0,
            pattern=PATTERN_LINEAR,
        )
        sub = _SubOrder(chunk_idx=0, quantity=1, price=8.4)
        nxt = _next_price(c, sub)
        assert nxt == pytest.approx(8.45)

    def test_sell_clamps_at_floor(self):
        c = _make_campaign(pattern=PATTERN_LINEAR)
        sub = _SubOrder(chunk_idx=0, quantity=1, price=8.4)
        # Already at floor for sell
        nxt = _next_price(c, sub)
        assert nxt is None

    def test_buy_clamps_at_ceiling(self):
        c = _make_campaign(
            instruction="BUY_TO_OPEN",
            range_start=8.4,
            range_end=9.0,
            pattern=PATTERN_LINEAR,
        )
        sub = _SubOrder(chunk_idx=0, quantity=1, price=9.0)
        nxt = _next_price(c, sub)
        assert nxt is None

    def test_random_walk_stays_in_range_over_many_iterations(self):
        c = _make_campaign(pattern=PATTERN_RANDOM_WALK, rng_seed=42)
        sub = _SubOrder(chunk_idx=0, quantity=1, price=9.0)
        for _ in range(200):
            nxt = _next_price(c, sub)
            if nxt is None:
                break
            assert c.range_end <= nxt <= c.range_start + c.step  # head-fake permits slight overshoot
            sub.price = nxt
        # Eventually should reach the floor under steady downward bias
        # (200 iterations is plenty)
        assert sub.price <= c.range_start

    def test_random_walk_eventually_progresses_toward_end(self):
        c = _make_campaign(pattern=PATTERN_RANDOM_WALK, rng_seed=1)
        sub = _SubOrder(chunk_idx=0, quantity=1, price=9.0)
        # Bias is 75% toward end; after many iterations, price should approach range_end
        for _ in range(500):
            nxt = _next_price(c, sub)
            if nxt is None:
                break
            sub.price = nxt
        # We should have moved at least halfway toward the end
        assert sub.price < 9.0 - (9.0 - 8.4) / 2


class TestSelectSubs:
    def test_linear_selects_all_live(self):
        c = _make_campaign(pattern=PATTERN_LINEAR)
        c.sub_orders = [
            _SubOrder(chunk_idx=0, quantity=2, price=9.0, schwab_status="WORKING"),
            _SubOrder(chunk_idx=1, quantity=2, price=9.0, schwab_status="FILLED"),
            _SubOrder(chunk_idx=2, quantity=2, price=9.0, schwab_status="WORKING"),
        ]
        selected = _select_subs_to_adjust(c)
        assert len(selected) == 2
        assert all(s.schwab_status == "WORKING" for s in selected)

    def test_random_walk_picks_subset(self):
        c = _make_campaign(pattern=PATTERN_RANDOM_WALK, rng_seed=99)
        c.sub_orders = [_SubOrder(chunk_idx=i, quantity=1, price=9.0, schwab_status="WORKING") for i in range(5)]
        selected = _select_subs_to_adjust(c)
        assert 1 <= len(selected) <= 2

    def test_no_live_subs(self):
        c = _make_campaign(pattern=PATTERN_RANDOM_WALK)
        c.sub_orders = [
            _SubOrder(chunk_idx=0, quantity=1, price=9.0, schwab_status="FILLED"),
        ]
        assert _select_subs_to_adjust(c) == []


class TestJitteredInterval:
    def test_zero_jitter_returns_base(self):
        c = _make_campaign(timing_jitter_pct=0.0)
        c.step_interval_seconds = 300.0
        assert _jittered_interval(c) == 300.0

    def test_jitter_within_bounds(self):
        c = _make_campaign(timing_jitter_pct=0.5, rng_seed=7)
        c.step_interval_seconds = 200.0
        for _ in range(100):
            v = _jittered_interval(c)
            # Allow for floor enforcement
            assert v >= 30.0
            assert v <= 200.0 * 1.5 + 0.01


# ===== Integration tests (with mock schwab client) =====


class _FakeOrdersClient:
    """Minimal fake of the schwab orders client.

    Tracks placed orders + cancel calls and lets tests control returned
    statuses. Does not use real HTTP, just records and returns.
    """

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.canceled: list[str] = []
        self.statuses: dict[str, dict[str, Any]] = {}
        self._next_id = 1000

    async def place_order(self, account_hash: str, order_spec: dict[str, Any]):
        oid = str(self._next_id)
        self._next_id += 1
        self.placed.append({"order_id": oid, "account_hash": account_hash, "spec": order_spec})
        self.statuses[oid] = {"orderId": oid, "status": "WORKING", "filledQuantity": 0}

        # Build a fake response that the response_handler can parse.
        class _Resp:
            def __init__(self, oid, ah):
                self.status_code = 201
                self.url = f"https://api.schwabapi.com/trader/v1/accounts/{ah}/orders"
                self.text = ""
                self.content = b""
                self.headers = {"Location": f"https://api.schwabapi.com/trader/v1/accounts/{ah}/orders/{oid}"}
                self.is_error = False

            def raise_for_status(self):
                return None

        return _Resp(oid, account_hash)

    async def cancel_order(self, order_id: str, account_hash: str):
        self.canceled.append(order_id)
        if order_id in self.statuses:
            self.statuses[order_id]["status"] = "CANCELED"

    async def get_order(self, order_id: str, account_hash: str):
        return self.statuses.get(order_id, {"orderId": order_id, "status": "UNKNOWN", "filledQuantity": 0})


def _patch_call_to_passthrough(monkeypatch):
    """Replace fishing.call() so the response_handler from orders.py is bypassed
    and our fake _FakeOrdersClient methods return values directly."""

    async def passthrough(func, *args, **kwargs):
        rh = kwargs.pop("response_handler", None)
        result = await func(*args, **kwargs)
        if rh is not None:
            # Our fake place_order returns a fake response; let the handler parse it
            ok, payload = rh(result)
            return payload if ok else None
        return result

    monkeypatch.setattr(fishing, "call", passthrough)


class TestIntegration:
    """Integration tests that exercise the campaign tools end-to-end (with mocked schwab client)."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        fishing._CAMPAIGNS.clear()
        yield
        fishing._CAMPAIGNS.clear()

    def test_validation_failure_raises(self, monkeypatch):
        _patch_call_to_passthrough(monkeypatch)
        client = _FakeOrdersClient()
        ctx = make_ctx(client)

        with pytest.raises(ValueError, match="SELL: range_start"):
            run(
                fishing.place_option_order_with_fishing(
                    ctx,
                    account_hash="abcd1234",
                    symbol="COIN  260717C00250000",
                    quantity=8,
                    instruction="SELL_TO_OPEN",
                    range_start=8.4,  # wrong direction for SELL
                    range_end=9.0,
                )
            )

    def test_place_and_cancel_immediately(self, monkeypatch):
        """Submit a campaign, immediately cancel it; should clean up gracefully."""
        _patch_call_to_passthrough(monkeypatch)
        # Make the step_interval very long so the bg loop is in its initial sleep
        # by the time we cancel.
        client = _FakeOrdersClient()
        ctx = make_ctx(client)

        async def run_test():
            result = await fishing.place_option_order_with_fishing(
                ctx,
                account_hash="0123456789ABCDEFsuffix1234",
                symbol="COIN  260717C00250000",
                quantity=4,
                instruction="SELL_TO_OPEN",
                range_start=9.0,
                range_end=8.4,
                step=0.05,
                pattern="linear",
                chunks=[2, 2],
                step_interval_seconds=3600.0,  # 1 hour; cancel before tick
                timing_jitter_pct=0.0,
            )
            cid = cast(dict[str, Any], result)["campaign_id"]

            # Wait a tick for the initial placement to complete
            await asyncio.sleep(0.05)

            # Verify two initial orders placed
            assert len(client.placed) == 2

            # Cancel
            final = await fishing.cancel_fishing(ctx, cid)
            assert cast(dict[str, Any], final)["status"] == STATUS_CANCELED
            # Both initial orders should be in canceled list
            assert len(client.canceled) == 2

        asyncio.run(run_test())

    def test_get_status_returns_campaign_state(self, monkeypatch):
        _patch_call_to_passthrough(monkeypatch)
        client = _FakeOrdersClient()
        ctx = make_ctx(client)

        async def run_test():
            result = await fishing.place_option_order_with_fishing(
                ctx,
                account_hash="0123456789ABCDEFsuffix1234",
                symbol="COIN  260717C00250000",
                quantity=3,
                instruction="SELL_TO_OPEN",
                range_start=9.0,
                range_end=8.4,
                step_interval_seconds=3600.0,
                chunks=[1, 1, 1],
                pattern="linear",
            )
            cid = cast(dict[str, Any], result)["campaign_id"]
            await asyncio.sleep(0.05)

            status = await fishing.get_fishing_status(ctx, cid)
            assert cast(dict[str, Any], status)["status"] == STATUS_RUNNING
            assert cast(dict[str, Any], status)["quantity"] == 3
            assert len(cast(dict[str, Any], status)["sub_orders"]) == 3
            assert cast(dict[str, Any], status)["symbol"] == "COIN  260717C00250000"

            # Cleanup
            await fishing.cancel_fishing(ctx, cid)

        asyncio.run(run_test())

    def test_status_for_unknown_campaign_raises(self, monkeypatch):
        _patch_call_to_passthrough(monkeypatch)
        ctx = make_ctx(_FakeOrdersClient())
        with pytest.raises(ValueError, match="Unknown campaign_id"):
            run(fishing.get_fishing_status(ctx, "nonexistent"))

    def test_terminal_filled_detected(self, monkeypatch):
        """Mark every placed order as immediately filled; the loop should detect terminal."""
        _patch_call_to_passthrough(monkeypatch)
        client = _FakeOrdersClient()
        ctx = make_ctx(client)

        # Wrap place_order so each placement is recorded as FILLED before the
        # next poll cycle sees it. This avoids a race where the loop keeps
        # canceling and replacing faster than the test can mark a specific
        # order id.
        original_place = client.place_order

        async def auto_fill_place(account_hash, order_spec):
            response = await original_place(account_hash, order_spec)
            latest_oid = client.placed[-1]["order_id"]
            # qty is in order_spec under orderLegCollection but easier to peek via spec
            qty = 0
            for leg in (order_spec or {}).get("orderLegCollection", []) or []:
                qty = max(qty, int(leg.get("quantity", 0) or 0))
            client.statuses[latest_oid] = {
                "orderId": latest_oid,
                "status": "FILLED",
                "filledQuantity": qty or 1,
            }
            return response

        client.place_order = auto_fill_place  # type: ignore[assignment]

        original_sleep = asyncio.sleep

        async def fast_sleep(secs):
            await original_sleep(0.01)

        monkeypatch.setattr(fishing.asyncio, "sleep", fast_sleep)

        async def run_test():
            result = await fishing.place_option_order_with_fishing(
                ctx,
                account_hash="0123456789ABCDEFsuffix1234",
                symbol="COIN  260717C00250000",
                quantity=2,
                instruction="SELL_TO_OPEN",
                range_start=9.0,
                range_end=8.4,
                step=0.05,
                pattern="linear",
                chunks=[2],
                step_interval_seconds=30.0,
                timing_jitter_pct=0.0,
            )
            cid = cast(dict[str, Any], result)["campaign_id"]

            # Wait for loop to detect terminal
            for _ in range(50):
                await original_sleep(0.05)
                if fishing._CAMPAIGNS[cid].status == STATUS_FILLED:
                    break

            final = fishing._CAMPAIGNS[cid].to_status_dict()
            assert cast(dict[str, Any], final)["status"] == STATUS_FILLED
            assert cast(dict[str, Any], final)["filled_quantity_total"] == 2

        asyncio.run(run_test())


# ===== Signal renderer integration =====


class TestSignalRenderer:
    def test_place_option_order_with_fishing_rendered(self):
        from schwab_mcp.approvals.signal import _TOOL_RENDERERS

        renderer = _TOOL_RENDERERS["place_option_order_with_fishing"]
        msg = renderer(
            {
                "account_hash": "…213E",
                "symbol": "COIN  260717C00250000",
                "quantity": 8,
                "instruction": "SELL_TO_OPEN",
                "range_start": 9.0,
                "range_end": 8.4,
                "step": 0.05,
                "pattern": "random_walk",
                "chunks": [3, 2, 2, 1],
                "step_interval_seconds": 300,
                "timing_jitter_pct": 0.4,
            },
            {"213E": "Unmanaged Trust"},
        )
        assert "sell to open" in msg
        assert "8" in msg
        assert "COIN" in msg
        assert "07/17/2026" in msg
        assert "$250" in msg
        assert "Call" in msg
        assert "Unmanaged Trust" in msg
        assert "fishing" in msg.lower()
        assert "random_walk" in msg
        assert "3/2/2/1" in msg

    def test_cancel_fishing_rendered(self):
        from schwab_mcp.approvals.signal import _TOOL_RENDERERS

        renderer = _TOOL_RENDERERS["cancel_fishing"]
        msg = renderer({"campaign_id": "abc-123"}, {})
        assert "abc-123" in msg
        assert "cancel" in msg.lower()
