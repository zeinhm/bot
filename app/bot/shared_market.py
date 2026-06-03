from __future__ import annotations

import logging

from binance import AsyncClient, BinanceSocketManager

from config import SYMBOLS

log = logging.getLogger(__name__)


class SharedMarketData:
    def __init__(self):
        self.market_client: AsyncClient | None = None
        self.market_bsm: BinanceSocketManager | None = None
        self._connected = False
        self._latest_prices: dict[str, float] = {}

    async def connect(self):
        if self._connected:
            return
        self.market_client = await AsyncClient.create(api_key="", api_secret="", testnet=False)
        self.market_bsm = BinanceSocketManager(self.market_client)
        self._connected = True
        log.info("SharedMarketData: connected to Binance public API")

    async def close(self):
        if self.market_client:
            await self.market_client.close_connection()
            self._connected = False
            log.info("SharedMarketData: disconnected")

    def get_latest_price(self, symbol: str) -> float:
        return self._latest_prices.get(symbol, 0.0)

    def update_price(self, symbol: str, price: float):
        self._latest_prices[symbol] = price

    @property
    def connected(self) -> bool:
        return self._connected
