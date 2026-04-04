from __future__ import annotations

from typing import Any

import httpx
import structlog

from src.config import Settings

logger = structlog.get_logger(__name__)


class PolymarketRestClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.polymarket_api_url
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_markets(self, active: bool = True, closed: bool = False) -> list[dict[str, Any]]:
        try:
            params = {"active": str(active).lower(), "closed": str(closed).lower()}
            response = await self.client.get(f"{self.base_url}/markets", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("markets_fetch_failed", error=str(e))
            raise

    async def get_orderbook(self, market_id: str) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}/book", params={"market": market_id})
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("orderbook_fetch_failed", market_id=market_id, error=str(e))
            raise

    async def get_market_info(self, market_id: str) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}/markets/{market_id}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("market_info_fetch_failed", market_id=market_id, error=str(e))
            raise

    async def get_balances(self, address: str) -> dict[str, Any]:
        try:
            response = await self.client.get(f"{self.base_url}/balances", params={"user": address})
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("balances_fetch_failed", address=address, error=str(e))
            raise

    async def get_open_orders(self, address: str, market_id: str | None = None) -> list[dict[str, Any]]:
        try:
            params = {"user": address}
            if market_id:
                params["market"] = market_id
            response = await self.client.get(f"{self.base_url}/open-orders", params=params)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("open_orders_fetch_failed", address=address, error=str(e))
            raise

    async def close(self):
        await self.client.aclose()

