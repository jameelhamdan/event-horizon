"""
Forex stream — ECB Statistical Data Warehouse REST API (free, no key).

Stores rates as PriceTick with stream_key='forex'.
"""
import logging

import requests

from django.utils import timezone as dj_timezone

from .base import BaseStream, HEADERS
from .prices import save_price_ticks

logger = logging.getLogger(__name__)

# ECB series keys (currency vs EUR) mapped to (symbol, display name)
# New ECB Data Portal API uses currency code only in the path.
ECB_SERIES = {
    'USD': ('USD/EUR', 'US Dollar / Euro'),
    'JPY': ('JPY/EUR', 'Japanese Yen / Euro'),
    'GBP': ('GBP/EUR', 'British Pound / Euro'),
    'CNY': ('CNY/EUR', 'Chinese Yuan / Euro'),
    'CHF': ('CHF/EUR', 'Swiss Franc / Euro'),
}

# ECB replaced sdw-wsrest.ecb.europa.eu with data-api.ecb.europa.eu
ECB_BASE_URL = 'https://data-api.ecb.europa.eu/service/data/EXR'


def _fetch_series(currency: str) -> float | None:
    series_key = f'D.{currency}.EUR.SP00.A'
    url = f'{ECB_BASE_URL}/{series_key}'
    params = {'lastNObservations': '1', 'format': 'jsondata', 'detail': 'dataonly'}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        obs = (
            data.get('dataSets', [{}])[0]
            .get('series', {})
            .get('0:0:0:0:0', {})
            .get('observations', {})
        )
        if not obs:
            return None
        latest_key = max(obs.keys(), key=int)
        value = obs[latest_key][0]
        return float(value) if value is not None else None
    except Exception as exc:
        logger.warning(f'[forex] ECB {series_key}: {exc}')  # series_key is defined above
        return None


class ForexStream(BaseStream):
    stream_type = 'forex'

    def fetch(self) -> list[dict]:
        now = dj_timezone.now()
        records = []
        for currency, (symbol, name) in ECB_SERIES.items():
            value = _fetch_series(currency)
            if value is not None:
                records.append({
                    'symbol': symbol,
                    'stream_key': 'forex',
                    'name': name,
                    'value': value,
                    'change_pct': None,
                    'volume': None,
                    'occurred_at': now,
                })
        return records

    def save(self, records: list[dict]) -> int:
        return save_price_ticks(records)
