from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from schwab.client import AsyncClient

from schwab_mcp.approvals import ApprovalDecision, ApprovalManager, ApprovalRequest
from schwab_mcp.context import SchwabContext, SchwabServerContext


class DummyApprovalManager(ApprovalManager):
    async def require(self, request: ApprovalRequest) -> ApprovalDecision:  # noqa: ARG002
        return ApprovalDecision.APPROVED


class _DummySession:
    """Minimal MCP session stub for tests that invoke ctx.warning/log."""

    async def send_log_message(self, **kwargs: Any) -> None:  # noqa: ARG002
        pass


def make_ctx(client: Any) -> SchwabContext:
    lifespan_context = SchwabServerContext(
        client=cast(AsyncClient, client),
        approval_manager=DummyApprovalManager(),
    )
    request_context = SimpleNamespace(
        lifespan_context=lifespan_context,
        request_id="test-request-id",
        meta=None,
        session=_DummySession(),
    )
    return SchwabContext.model_construct(
        _request_context=cast(Any, request_context),
        _fastmcp=None,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def ctx_factory():
    return make_ctx


@pytest.fixture
def fake_call_capture():
    captured: dict[str, Any] = {}

    async def fake_call(func, *args, **kwargs):
        captured["func"] = func
        captured["args"] = args
        captured["kwargs"] = kwargs
        return "ok"

    return captured, fake_call


@pytest.fixture
def fake_call_factory():
    """Factory fixture for creating fake call mocks with optional return values.

    Returns a factory function that creates (captured_dict, fake_call) tuples.
    The fake_call function captures function calls for test assertions.

    Args:
        return_value: Optional value to return from fake_call (default: "ok")

    Returns:
        Tuple of (captured dict, async fake_call function)
    """

    def factory(return_value: Any = "ok"):
        captured: dict[str, Any] = {}

        async def fake_call(func, *args, **kwargs):
            captured["func"] = func
            captured["args"] = args
            captured["kwargs"] = kwargs
            return return_value

        return captured, fake_call

    return factory


class DummyOrderResponse:
    """Mock HTTP response for order placement."""

    def __init__(self, account_hash: str = "default_hash", order_id: int = 123456789):
        self.status_code = 201
        self.url = f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders"
        self.text = ""
        self.content = b""
        self.headers = {"Location": f"https://api.schwabapi.com/trader/v1/accounts/{account_hash}/orders/{order_id}"}
        self.is_error = False

    def raise_for_status(self) -> None:
        """No-op method for compatibility with requests.Response."""
        return None


@pytest.fixture
def order_response_factory():
    """Factory fixture for creating DummyOrderResponse instances."""

    def factory(account_hash: str = "default_hash", order_id: int = 123456789):
        return DummyOrderResponse(account_hash=account_hash, order_id=order_id)

    return factory


class DummyInstrumentsResponse:
    """Mock HTTP response for get_instruments lookup.

    Defaults to a single EQUITY-typed instrument so order tests that do not
    explicitly exercise the asset-type validator continue to pass through.
    """

    def __init__(self, symbol: str = "TEST", asset_type: str | None = "EQUITY") -> None:
        self.status_code = 200
        instruments: list[dict[str, Any]] = []
        if asset_type is not None:
            instruments.append({"symbol": symbol, "assetType": asset_type})
        self._payload = {"instruments": instruments}
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class DummyPlaceOrderClient:
    """Mock client for place_order() method testing.

    Also stubs `get_instruments` so the upfront asset-type validator added by
    issue #29 returns EQUITY for any looked-up symbol by default. Tests that
    want to exercise the rejection path can set
    `client.asset_type_override = "MUTUAL_FUND"` (or any other value), or set
    `client.asset_type_override = "raise"` to simulate a lookup failure.
    """

    def __init__(self, order_response: Any):
        self.captured: dict[str, Any] | None = None
        self._response = order_response
        self.asset_type_override: str | None = "EQUITY"

    async def place_order(self, *args: Any, **kwargs: Any) -> Any:
        """Capture call arguments and return mock response."""
        self.captured = {"args": args, "kwargs": kwargs}
        return self._response

    async def get_instruments(self, symbol: str, projection: Any) -> Any:  # noqa: ARG002
        if self.asset_type_override == "raise":
            raise RuntimeError("simulated get_instruments failure")
        return DummyInstrumentsResponse(symbol=symbol, asset_type=self.asset_type_override)


@pytest.fixture
def place_order_client_factory(order_response_factory):
    """Factory fixture for creating DummyPlaceOrderClient instances."""

    def factory(account_hash: str = "default_hash", order_id: int = 123456789):
        response = order_response_factory(account_hash=account_hash, order_id=order_id)
        return DummyPlaceOrderClient(order_response=response)

    return factory
