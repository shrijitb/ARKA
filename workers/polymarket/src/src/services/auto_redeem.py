from __future__ import annotations

from typing import Any

import httpx
import structlog

from src.config import Settings

logger = structlog.get_logger(__name__)


class AutoRedeem:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=30.0)

    async def check_redeemable_positions(self, address: str) -> list[dict[str, Any]]:
        try:
            response = await self.client.get(
                f"{self.settings.polymarket_api_url}/positions",
                params={"user": address, "redeemable": "true"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("redeemable_positions_check_failed", error=str(e))
            return []

    async def redeem_position(self, position_id: str) -> bool:
        try:
            response = await self.client.post(
                f"{self.settings.polymarket_api_url}/redeem/{position_id}",
            )
            response.raise_for_status()
            logger.info("position_redeemed", position_id=position_id)
            return True
        except Exception as e:
            logger.error("position_redeem_failed", position_id=position_id, error=str(e))
            return False

    async def auto_redeem_all(self, address: str) -> int:
        if not self.settings.auto_redeem_enabled:
            return 0
        
        redeemable = await self.check_redeemable_positions(address)
        redeemed = 0
        
        for position in redeemable:
            value_usd = float(position.get("value", 0))
            if value_usd >= self.settings.redeem_threshold_usd:
                if await self.redeem_position(position.get("id")):
                    redeemed += 1
        
        logger.info("auto_redeem_completed", redeemed=redeemed, total=len(redeemable))
        return redeemed

    async def close(self):
        await self.client.aclose()

