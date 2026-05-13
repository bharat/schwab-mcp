"""Range-based, chunked option-order fishing campaigns.

A "fishing" campaign places multiple LIMIT child orders ("chunks") across a
price range and then cancel/replaces them periodically toward the range's
terminal price, with optional randomized timing and price-walk patterns. The
goal is to harvest the best fill within a user-approved price range without
revealing the persistent intent of a single trader.

One Signal/Discord approval at submission covers the whole campaign. Subsequent
child cancel/place operations run autonomously inside the MCP server's event
loop. Campaigns are tracked in an in-memory registry and die with the server.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any, cast

from mcp.server.fastmcp import FastMCP

from schwab_mcp.context import SchwabContext
from schwab_mcp.tools._registration import register_tool
from schwab_mcp.tools.orders import _apply_order_settings, _build_option_order_spec
from schwab_mcp.tools.utils import JSONType, call

logger = logging.getLogger(__name__)


# ----- Constants -----

PATTERN_LINEAR = "linear"
PATTERN_RANDOM_WALK = "random_walk"
PATTERN_STAGGERED = "staggered"
_PATTERNS = frozenset({PATTERN_LINEAR, PATTERN_RANDOM_WALK, PATTERN_STAGGERED})

STATUS_RUNNING = "RUNNING"
STATUS_FILLED = "FILLED"
STATUS_CANCELED = "CANCELED"
STATUS_FAILED = "FAILED"
STATUS_EXHAUSTED = "EXHAUSTED"

_OPTION_INSTRUCTIONS = frozenset(
    {"BUY_TO_OPEN", "SELL_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_CLOSE"}
)

_DEFAULT_STEP = 0.05
_DEFAULT_INTERVAL_SEC = 300.0
_DEFAULT_JITTER_PCT = 0.4
_MIN_INTERVAL_SEC = 30.0
_MAX_CAMPAIGN_SECONDS = 60 * 60 * 4  # 4 hours hard cap
_CANCEL_GRACE_SECONDS = 10


@dataclass
class _SubOrder:
    """State for one chunk's current order."""

    chunk_idx: int
    quantity: int
    price: float
    order_id: str | None = None
    schwab_status: str = "PENDING"
    filled_quantity: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def is_terminal(self) -> bool:
        return self.schwab_status in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}

    def is_working(self) -> bool:
        return self.schwab_status == "WORKING"


@dataclass
class _Campaign:
    """In-memory state for one fishing campaign."""

    id: str
    account_hash: str
    symbol: str
    instruction: str
    quantity: int
    chunks: list[int]
    range_start: float
    range_end: float
    step: float
    pattern: str
    step_interval_seconds: float
    timing_jitter_pct: float
    session: str
    duration: str
    started_at: datetime
    status: str
    sub_orders: list[_SubOrder] = field(default_factory=list)
    last_event: str | None = None
    task: asyncio.Task[None] | None = None
    rng: random.Random = field(default_factory=random.Random)

    def is_sell(self) -> bool:
        return self.instruction.startswith("SELL")

    def to_status_dict(self) -> dict[str, Any]:
        total_filled = sum(s.filled_quantity for s in self.sub_orders)
        return {
            "id": self.id,
            "status": self.status,
            "account_hash_suffix": self.account_hash[-4:],
            "symbol": self.symbol,
            "instruction": self.instruction,
            "quantity": self.quantity,
            "filled_quantity_total": total_filled,
            "chunks": list(self.chunks),
            "range_start": self.range_start,
            "range_end": self.range_end,
            "step": self.step,
            "pattern": self.pattern,
            "step_interval_seconds": self.step_interval_seconds,
            "timing_jitter_pct": self.timing_jitter_pct,
            "session": self.session,
            "duration": self.duration,
            "started_at": self.started_at.isoformat(),
            "last_event": self.last_event,
            "sub_orders": [
                {
                    "chunk_idx": s.chunk_idx,
                    "quantity": s.quantity,
                    "price": s.price,
                    "order_id": s.order_id,
                    "schwab_status": s.schwab_status,
                    "filled_quantity": s.filled_quantity,
                    "history_count": len(s.history),
                }
                for s in self.sub_orders
            ],
        }


# Module-level registry. In-memory only; dies with the server process.
_CAMPAIGNS: dict[str, _Campaign] = {}
_CAMPAIGNS_LOCK = asyncio.Lock()


# ===== Input validation =====


def _auto_chunks(quantity: int) -> list[int]:
    """Auto-split a total quantity into 2-4 chunks, near-evenly."""
    n = min(4, max(2, quantity // 3 or 1))
    if quantity < n:
        n = quantity
    if n <= 0:
        return [quantity]
    base = quantity // n
    rem = quantity % n
    chunks = [base + (1 if i < rem else 0) for i in range(n)]
    return [c for c in chunks if c > 0]


def _validate_inputs(
    instruction: str,
    quantity: int,
    range_start: float,
    range_end: float,
    step: float,
    pattern: str,
    chunks: list[int] | None,
    step_interval_seconds: float,
    timing_jitter_pct: float,
) -> tuple[str, list[int], str]:
    instruction = instruction.upper()
    if instruction not in _OPTION_INSTRUCTIONS:
        raise ValueError(
            f"instruction must be one of {sorted(_OPTION_INSTRUCTIONS)}; got {instruction!r}"
        )

    is_sell = instruction.startswith("SELL")
    if is_sell and not (range_start > range_end):
        raise ValueError(
            f"For SELL: range_start ({range_start}) must be > range_end ({range_end})"
        )
    if (not is_sell) and not (range_start < range_end):
        raise ValueError(
            f"For BUY: range_start ({range_start}) must be < range_end ({range_end})"
        )

    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    if step <= 0:
        raise ValueError("step must be > 0")
    if abs(range_start - range_end) < step:
        raise ValueError(
            f"range span ({abs(range_start - range_end):.4f}) is smaller than step ({step})"
        )

    pattern_normalized = pattern.lower()
    if pattern_normalized not in _PATTERNS:
        raise ValueError(f"pattern must be one of {sorted(_PATTERNS)}; got {pattern!r}")

    if step_interval_seconds < _MIN_INTERVAL_SEC:
        raise ValueError(
            f"step_interval_seconds must be >= {_MIN_INTERVAL_SEC}; got {step_interval_seconds}"
        )
    if not (0.0 <= timing_jitter_pct <= 1.0):
        raise ValueError(
            f"timing_jitter_pct must be in [0, 1]; got {timing_jitter_pct}"
        )

    if chunks is None:
        chunks_resolved = _auto_chunks(quantity)
    else:
        if not chunks:
            raise ValueError("chunks must be non-empty if provided")
        if any(c <= 0 for c in chunks):
            raise ValueError("each chunk must be > 0")
        if sum(chunks) != quantity:
            raise ValueError(f"sum(chunks)={sum(chunks)} != quantity={quantity}")
        chunks_resolved = list(chunks)

    return instruction, chunks_resolved, pattern_normalized


# ===== Price-pattern logic =====


def _initial_prices(campaign: _Campaign) -> list[float]:
    """Initial limit prices for each chunk."""
    n = len(campaign.chunks)
    if n == 0:
        return []

    if campaign.pattern == PATTERN_LINEAR:
        return [campaign.range_start] * n

    if campaign.pattern == PATTERN_STAGGERED:
        # Linear ladder over ~40% of the range, so all chunks start in the
        # "favorable half" of the range.
        span = campaign.range_start - campaign.range_end
        prices = [
            campaign.range_start - (span * i / max(n - 1, 1)) * 0.4 for i in range(n)
        ]
        # Clamp to range
        if campaign.is_sell():
            prices = [max(p, campaign.range_end) for p in prices]
        else:
            prices = [min(p, campaign.range_end) for p in prices]
        return [round(p, 2) for p in prices]

    # random_walk: scatter near range_start with up to ~3 steps of noise
    prices: list[float] = []
    for _ in range(n):
        jitter_steps = campaign.rng.uniform(0, 3)
        jitter = jitter_steps * campaign.step
        if campaign.is_sell():
            p = campaign.range_start - jitter
            p = max(p, campaign.range_end)
        else:
            p = campaign.range_start + jitter
            p = min(p, campaign.range_end)
        prices.append(round(p, 2))
    return prices


def _next_price(campaign: _Campaign, sub: _SubOrder) -> float | None:
    """Compute next adjusted price for a sub-order, or None if at range_end."""
    is_sell = campaign.is_sell()
    cur = sub.price
    end = campaign.range_end
    step = campaign.step

    # Already at/past floor?
    if is_sell and cur <= end:
        return None
    if (not is_sell) and cur >= end:
        return None

    if campaign.pattern == PATTERN_LINEAR or campaign.pattern == PATTERN_STAGGERED:
        nxt = cur - step if is_sell else cur + step
    else:
        # random_walk: bias toward end (75%), stay (20%), head-fake (5%)
        r = campaign.rng.random()
        if r < 0.75:
            base = cur - step if is_sell else cur + step
            # Random step magnitude 50%-150% of base step
            scale = 0.5 + campaign.rng.random()
            delta = (base - cur) * scale
            nxt = cur + delta
        elif r < 0.95:
            nxt = cur
        else:
            # head-fake — step BACK toward range_start by a fraction of step
            nxt = cur + step * 0.5 if is_sell else cur - step * 0.5

    # Clamp to range
    if is_sell:
        nxt = max(nxt, end)
    else:
        nxt = min(nxt, end)

    return round(nxt, 2)


def _select_subs_to_adjust(campaign: _Campaign) -> list[_SubOrder]:
    """Pick which sub-orders to adjust this tick."""
    candidates = [s for s in campaign.sub_orders if not s.is_terminal()]
    if not candidates:
        return []
    if campaign.pattern == PATTERN_LINEAR:
        return candidates
    if len(candidates) == 1:
        return candidates
    # random_walk / staggered: pick 1-2
    n = campaign.rng.choice([1, 1, 2])
    n = min(n, len(candidates))
    return campaign.rng.sample(candidates, n)


def _jittered_interval(campaign: _Campaign) -> float:
    base = campaign.step_interval_seconds
    j = campaign.timing_jitter_pct
    if j <= 0:
        return base
    factor = 1.0 + campaign.rng.uniform(-j, j)
    return max(_MIN_INTERVAL_SEC, base * factor)


# ===== Direct schwab client ops (no approval wrapper) =====


def _make_response_handler(
    client: Any, account_hash: str
) -> Callable[[Any], tuple[bool, Any]]:
    """Build a response handler that extracts the order_id from a Schwab response."""
    from schwab.utils import (
        AccountHashMismatchException,
        UnsuccessfulOrderException,
        Utils as SchwabUtils,
    )

    utils = SchwabUtils(client, account_hash)

    def handler(response: Any) -> tuple[bool, Any]:
        headers = getattr(response, "headers", {})
        location = headers.get("Location") if headers else None
        try:
            order_id = utils.extract_order_id(response)
        except (AccountHashMismatchException, UnsuccessfulOrderException):
            order_id = None
        if order_id is None and location is None:
            return False, None
        payload: dict[str, Any] = {}
        if order_id is not None:
            payload["orderId"] = str(order_id)
            payload["accountHash"] = account_hash
        if location is not None:
            payload["location"] = location
        return True, payload

    return handler


async def _direct_place_option(
    client: Any,
    account_hash: str,
    symbol: str,
    quantity: int,
    instruction: str,
    price: float,
    session: str,
    duration: str,
    response_handler: Callable[[Any], tuple[bool, Any]],
) -> str | None:
    spec_builder = _build_option_order_spec(
        symbol, quantity, instruction, "LIMIT", price=price
    )
    spec_builder = _apply_order_settings(spec_builder, session, duration)
    spec = cast(dict[str, Any], spec_builder.build())
    result = await call(
        client.place_order,
        account_hash=account_hash,
        order_spec=spec,
        response_handler=response_handler,
    )
    if isinstance(result, dict):
        oid = result.get("orderId")
        return str(oid) if oid is not None else None
    return None


async def _direct_cancel(client: Any, account_hash: str, order_id: str) -> None:
    try:
        await call(client.cancel_order, order_id=order_id, account_hash=account_hash)
    except Exception:  # pragma: no cover - best-effort cancel
        logger.exception("Fishing: failed to cancel order %s", order_id)


async def _direct_get_status(
    client: Any, account_hash: str, order_id: str
) -> dict[str, Any] | None:
    try:
        result = await call(
            client.get_order, order_id=order_id, account_hash=account_hash
        )
        if isinstance(result, dict):
            return result
    except Exception:  # pragma: no cover - best-effort poll
        logger.exception("Fishing: failed to poll order %s", order_id)
    return None


# ===== Background loop =====


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _place_initial_chunks(client: Any, campaign: _Campaign) -> None:
    rh = _make_response_handler(client, campaign.account_hash)
    initial_prices = _initial_prices(campaign)
    for chunk_idx, (qty, price) in enumerate(zip(campaign.chunks, initial_prices)):
        sub = _SubOrder(chunk_idx=chunk_idx, quantity=qty, price=price)
        campaign.sub_orders.append(sub)
        try:
            oid = await _direct_place_option(
                client,
                campaign.account_hash,
                campaign.symbol,
                qty,
                campaign.instruction,
                price,
                campaign.session,
                campaign.duration,
                rh,
            )
            sub.order_id = oid
            sub.schwab_status = "WORKING" if oid else "FAILED"
            sub.history.append(
                {
                    "at": _now_iso(),
                    "event": "placed",
                    "price": price,
                    "order_id": oid,
                }
            )
        except Exception as e:
            sub.schwab_status = "FAILED"
            sub.history.append(
                {"at": _now_iso(), "event": "place_failed", "error": str(e)}
            )
            logger.exception(
                "Fishing %s chunk %d initial place failed", campaign.id, chunk_idx
            )


async def _poll_sub_statuses(client: Any, campaign: _Campaign) -> None:
    for sub in campaign.sub_orders:
        if sub.is_terminal() or sub.order_id is None:
            continue
        status_result = await _direct_get_status(
            client, campaign.account_hash, sub.order_id
        )
        if status_result is None:
            continue
        new_status = str(status_result.get("status", sub.schwab_status))
        try:
            filled = int(float(status_result.get("filledQuantity", 0)))
        except (TypeError, ValueError):
            filled = 0
        if new_status != sub.schwab_status or filled != sub.filled_quantity:
            sub.history.append(
                {
                    "at": _now_iso(),
                    "event": "status_update",
                    "old_status": sub.schwab_status,
                    "new_status": new_status,
                    "filled_qty": filled,
                }
            )
            sub.schwab_status = new_status
            sub.filled_quantity = filled


def _check_terminal(campaign: _Campaign) -> bool:
    """Returns True if the campaign reached a terminal state; mutates campaign.status."""
    if all(s.is_terminal() for s in campaign.sub_orders):
        total_filled = sum(s.filled_quantity for s in campaign.sub_orders)
        any_filled = any(s.schwab_status == "FILLED" for s in campaign.sub_orders)
        if total_filled >= campaign.quantity:
            campaign.status = STATUS_FILLED
            campaign.last_event = f"all chunks filled (total {total_filled})"
        elif any_filled:
            campaign.status = STATUS_FILLED
            campaign.last_event = f"all chunks terminal, partial fill ({total_filled}/{campaign.quantity})"
        else:
            campaign.status = STATUS_EXHAUSTED
            campaign.last_event = "all chunks terminal with no fills"
        return True
    return False


async def _adjust_subs(client: Any, campaign: _Campaign) -> int:
    """Cancel + re-place selected sub-orders. Returns count adjusted."""
    adjustable = [s for s in campaign.sub_orders if not s.is_terminal()]
    if not adjustable:
        return 0

    # All live subs at the floor?
    at_floor = [s for s in adjustable if _next_price(campaign, s) is None]
    if len(at_floor) == len(adjustable):
        campaign.status = STATUS_EXHAUSTED
        campaign.last_event = "all live chunks at range_end with no fill"
        return 0

    rh = _make_response_handler(client, campaign.account_hash)
    to_adjust = _select_subs_to_adjust(campaign)
    count = 0
    for sub in to_adjust:
        new_price = _next_price(campaign, sub)
        if new_price is None or new_price == sub.price:
            continue
        old_oid = sub.order_id
        if old_oid is not None:
            await _direct_cancel(client, campaign.account_hash, old_oid)
            sub.history.append(
                {
                    "at": _now_iso(),
                    "event": "canceled_for_replace",
                    "order_id": old_oid,
                    "old_price": sub.price,
                }
            )
        try:
            new_oid = await _direct_place_option(
                client,
                campaign.account_hash,
                campaign.symbol,
                sub.quantity,
                campaign.instruction,
                new_price,
                campaign.session,
                campaign.duration,
                rh,
            )
            sub.order_id = new_oid
            sub.price = new_price
            sub.schwab_status = "WORKING" if new_oid else "FAILED"
            sub.history.append(
                {
                    "at": _now_iso(),
                    "event": "replaced",
                    "price": new_price,
                    "order_id": new_oid,
                }
            )
            count += 1
        except Exception as e:
            sub.schwab_status = "FAILED"
            sub.history.append(
                {"at": _now_iso(), "event": "replace_failed", "error": str(e)}
            )
            logger.exception(
                "Fishing %s chunk %d replace failed", campaign.id, sub.chunk_idx
            )
    return count


async def _cleanup_open_orders(client: Any, campaign: _Campaign) -> None:
    for sub in campaign.sub_orders:
        if sub.is_working() and sub.order_id is not None:
            await _direct_cancel(client, campaign.account_hash, sub.order_id)
            sub.schwab_status = "CANCELED"
            sub.history.append({"at": _now_iso(), "event": "canceled_at_termination"})


async def _run_campaign(client: Any, campaign: _Campaign) -> None:
    """Background task: place initial chunks, then poll + adjust until terminal."""
    logger.info("Fishing campaign %s starting", campaign.id)
    try:
        await _place_initial_chunks(client, campaign)
        campaign.last_event = "initial orders placed"

        loop = asyncio.get_event_loop()
        deadline = loop.time() + _MAX_CAMPAIGN_SECONDS

        while campaign.status == STATUS_RUNNING:
            if loop.time() > deadline:
                campaign.last_event = "max campaign duration reached"
                campaign.status = STATUS_EXHAUSTED
                break

            sleep_secs = _jittered_interval(campaign)
            try:
                await asyncio.sleep(sleep_secs)
            except asyncio.CancelledError:
                campaign.last_event = "task canceled"
                campaign.status = STATUS_CANCELED
                break

            await _poll_sub_statuses(client, campaign)
            if _check_terminal(campaign):
                break

            adjusted = await _adjust_subs(client, campaign)
            if campaign.status != STATUS_RUNNING:
                break
            campaign.last_event = (
                f"poll cycle complete; adjusted {adjusted} chunk(s) @ {_now_iso()}"
            )

        await _cleanup_open_orders(client, campaign)
    except Exception:
        logger.exception("Fishing campaign %s crashed", campaign.id)
        campaign.status = STATUS_FAILED
        campaign.last_event = "campaign crashed; see server logs"
        try:
            await _cleanup_open_orders(client, campaign)
        except Exception:
            logger.exception(
                "Fishing campaign %s: cleanup after crash also failed", campaign.id
            )
    finally:
        logger.info(
            "Fishing campaign %s ended with status %s", campaign.id, campaign.status
        )


# ===== Public tools =====


async def place_option_order_with_fishing(
    ctx: SchwabContext,
    account_hash: Annotated[str, "Account hash for the Schwab account"],
    symbol: Annotated[str, "Option symbol (OCC format, e.g. 'COIN  260717C00250000')"],
    quantity: Annotated[int, "Total number of contracts across all chunks combined"],
    instruction: Annotated[
        str,
        "Option instruction: BUY_TO_OPEN, SELL_TO_OPEN, BUY_TO_CLOSE, or SELL_TO_CLOSE",
    ],
    range_start: Annotated[
        float,
        "Initial (most favorable) limit price. For SELL > range_end; for BUY < range_end.",
    ],
    range_end: Annotated[
        float,
        "Terminal (least favorable) limit price. The campaign stops adjusting past this.",
    ],
    step: Annotated[
        float | None, "Price step per adjustment (default 0.05)"
    ] = _DEFAULT_STEP,
    pattern: Annotated[
        str | None,
        "Step pattern: 'random_walk' (default, randomized + head-fakes), 'linear' (all chunks step together), or 'staggered' (initial ladder).",
    ] = PATTERN_RANDOM_WALK,
    chunks: Annotated[
        list[int] | None,
        "Optional chunk sizes summing to quantity (e.g. [3,2,2,1] for total=8). Default auto-splits into 2-4 chunks.",
    ] = None,
    step_interval_seconds: Annotated[
        float | None,
        f"Base seconds between adjustments (default {int(_DEFAULT_INTERVAL_SEC)}; min {int(_MIN_INTERVAL_SEC)}).",
    ] = _DEFAULT_INTERVAL_SEC,
    timing_jitter_pct: Annotated[
        float | None,
        "±jitter on interval as a fraction (0.0-1.0; default 0.4 ≈ ±40%). 0 = perfectly metronomic.",
    ] = _DEFAULT_JITTER_PCT,
    session: Annotated[
        str | None, "Trading session: NORMAL (default), AM, PM, or SEAMLESS"
    ] = "NORMAL",
    duration: Annotated[
        str | None, "Order duration: DAY (default), GOOD_TILL_CANCEL"
    ] = "DAY",
) -> JSONType:
    """
    Place a chunked, range-fishing option campaign with a single approval.

    The campaign places multiple LIMIT child orders ("chunks") across a price
    range, then cancel/replaces them periodically toward range_end with
    randomized timing and price-walk patterns to obfuscate the single-trader
    intent. ONE approval at submission covers the whole campaign — subsequent
    child cancel/place operations run autonomously.

    Returns a campaign descriptor including campaign_id. Poll get_fishing_status
    or abort via cancel_fishing.

    Params:
    - account_hash, symbol (OCC), quantity, instruction (BUY_TO_*/SELL_TO_*).
    - range_start (most favorable price), range_end (terminal stop price).
    - step: price increment (default 0.05).
    - pattern: random_walk (default), linear, staggered.
    - chunks: optional list of chunk sizes summing to quantity (default auto 2-4).
    - step_interval_seconds: base seconds between adjustments (default 300).
    - timing_jitter_pct: 0.0-1.0; randomizes interval (default 0.4 = ±40%).

    Background task runs in the MCP server's event loop; dies on server restart
    (leftover orders are NOT auto-canceled on crash). 4-hour hard cap per
    campaign.

    *Write operation.* Approval required.
    """
    instr, chunks_resolved, pattern_resolved = _validate_inputs(
        instruction,
        quantity,
        float(range_start),
        float(range_end),
        float(step if step is not None else _DEFAULT_STEP),
        pattern or PATTERN_RANDOM_WALK,
        chunks,
        float(
            step_interval_seconds
            if step_interval_seconds is not None
            else _DEFAULT_INTERVAL_SEC
        ),
        float(
            timing_jitter_pct if timing_jitter_pct is not None else _DEFAULT_JITTER_PCT
        ),
    )

    campaign_id = str(uuid.uuid4())
    campaign = _Campaign(
        id=campaign_id,
        account_hash=account_hash,
        symbol=symbol,
        instruction=instr,
        quantity=quantity,
        chunks=chunks_resolved,
        range_start=float(range_start),
        range_end=float(range_end),
        step=float(step if step is not None else _DEFAULT_STEP),
        pattern=pattern_resolved,
        step_interval_seconds=float(
            step_interval_seconds
            if step_interval_seconds is not None
            else _DEFAULT_INTERVAL_SEC
        ),
        timing_jitter_pct=float(
            timing_jitter_pct if timing_jitter_pct is not None else _DEFAULT_JITTER_PCT
        ),
        session=session or "NORMAL",
        duration=duration or "DAY",
        started_at=datetime.now(timezone.utc),
        status=STATUS_RUNNING,
    )

    async with _CAMPAIGNS_LOCK:
        _CAMPAIGNS[campaign_id] = campaign

    client = ctx.client
    task = asyncio.create_task(
        _run_campaign(client, campaign), name=f"fishing-{campaign_id}"
    )
    campaign.task = task

    return {
        "campaign_id": campaign_id,
        "status": campaign.status,
        "chunks": list(chunks_resolved),
        "range_start": float(range_start),
        "range_end": float(range_end),
        "step": float(step if step is not None else _DEFAULT_STEP),
        "pattern": pattern_resolved,
        "step_interval_seconds": float(
            step_interval_seconds
            if step_interval_seconds is not None
            else _DEFAULT_INTERVAL_SEC
        ),
        "timing_jitter_pct": float(
            timing_jitter_pct if timing_jitter_pct is not None else _DEFAULT_JITTER_PCT
        ),
        "started_at": campaign.started_at.isoformat(),
    }


async def get_fishing_status(
    ctx: SchwabContext,
    campaign_id: Annotated[
        str, "Fishing campaign id returned by place_option_order_with_fishing"
    ],
) -> JSONType:
    """
    Returns the current status of a fishing campaign (chunk-level prices, statuses, fill counts).
    """
    _ = ctx  # unused but required for tool signature
    campaign = _CAMPAIGNS.get(campaign_id)
    if campaign is None:
        raise ValueError(f"Unknown campaign_id: {campaign_id}")
    return campaign.to_status_dict()


async def cancel_fishing(
    ctx: SchwabContext,
    campaign_id: Annotated[str, "Fishing campaign id to terminate"],
) -> JSONType:
    """
    Terminates a running fishing campaign: cancels the background task, then
    cancels any sub-orders still working. Returns the final campaign state.
    *Write operation.*
    """
    campaign = _CAMPAIGNS.get(campaign_id)
    if campaign is None:
        raise ValueError(f"Unknown campaign_id: {campaign_id}")

    task = campaign.task
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_CANCEL_GRACE_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        except Exception:
            logger.exception("Fishing %s: task error during cancel", campaign_id)

    # Defensive: also cancel any working orders directly.
    client = ctx.client
    for sub in campaign.sub_orders:
        if sub.is_working() and sub.order_id is not None:
            await _direct_cancel(client, campaign.account_hash, sub.order_id)
            sub.schwab_status = "CANCELED"
            sub.history.append({"at": _now_iso(), "event": "canceled_by_user"})

    if campaign.status == STATUS_RUNNING:
        campaign.status = STATUS_CANCELED
        campaign.last_event = "canceled by user via cancel_fishing"
    return campaign.to_status_dict()


# ===== Registration =====

_READ_ONLY_TOOLS = (get_fishing_status,)
_WRITE_TOOLS = (place_option_order_with_fishing, cancel_fishing)


def register(
    server: FastMCP,
    *,
    allow_write: bool,
    result_transform: Callable[[Any], Any] | None = None,
) -> None:
    for func in _READ_ONLY_TOOLS:
        register_tool(server, func, result_transform=result_transform)
    if not allow_write:
        return
    for func in _WRITE_TOOLS:
        register_tool(server, func, write=True, result_transform=result_transform)


__all__ = [
    "register",
    "place_option_order_with_fishing",
    "get_fishing_status",
    "cancel_fishing",
    "PATTERN_LINEAR",
    "PATTERN_RANDOM_WALK",
    "PATTERN_STAGGERED",
    "STATUS_RUNNING",
    "STATUS_FILLED",
    "STATUS_CANCELED",
    "STATUS_FAILED",
    "STATUS_EXHAUSTED",
]
