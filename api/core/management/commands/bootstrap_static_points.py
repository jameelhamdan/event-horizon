"""
Management command: bootstrap_static_points

Loads static reference points (exchanges, ports, central banks) into the DB.
Idempotent — safe to run multiple times.

Usage:
    python manage.py bootstrap_static_points
    python manage.py bootstrap_static_points --type exchange
"""
from django.core.management.base import BaseCommand
from core import models as core_models

STATIC_POINTS = [
    # ------------------------------------------------------------------ Stock Exchanges
    {'code': 'NYSE',   'point_type': 'exchange', 'name': 'New York Stock Exchange',        'country': 'United States', 'country_code': 'US', 'latitude': 40.7069,  'longitude': -74.0089, 'metadata': {'timezone': 'America/New_York', 'website': 'nyse.com'}},
    {'code': 'NASDAQ', 'point_type': 'exchange', 'name': 'NASDAQ',                         'country': 'United States', 'country_code': 'US', 'latitude': 40.7580,  'longitude': -73.9855, 'metadata': {'timezone': 'America/New_York', 'website': 'nasdaq.com'}},
    {'code': 'LSE',    'point_type': 'exchange', 'name': 'London Stock Exchange',           'country': 'United Kingdom', 'country_code': 'GB', 'latitude': 51.5142, 'longitude': -0.0969,  'metadata': {'timezone': 'Europe/London',   'website': 'londonstockexchange.com'}},
    {'code': 'TSE',    'point_type': 'exchange', 'name': 'Tokyo Stock Exchange',            'country': 'Japan',         'country_code': 'JP', 'latitude': 35.6840,  'longitude': 139.7692, 'metadata': {'timezone': 'Asia/Tokyo',      'website': 'jpx.co.jp'}},
    {'code': 'SSE',    'point_type': 'exchange', 'name': 'Shanghai Stock Exchange',         'country': 'China',         'country_code': 'CN', 'latitude': 31.2332,  'longitude': 121.4784, 'metadata': {'timezone': 'Asia/Shanghai',   'website': 'sse.com.cn'}},
    {'code': 'SZSE',   'point_type': 'exchange', 'name': 'Shenzhen Stock Exchange',         'country': 'China',         'country_code': 'CN', 'latitude': 22.5431,  'longitude': 114.0579, 'metadata': {'timezone': 'Asia/Shanghai',   'website': 'szse.cn'}},
    {'code': 'HKEX',   'point_type': 'exchange', 'name': 'Hong Kong Stock Exchange',        'country': 'Hong Kong',     'country_code': 'HK', 'latitude': 22.2819,  'longitude': 114.1581, 'metadata': {'timezone': 'Asia/Hong_Kong',  'website': 'hkex.com.hk'}},
    {'code': 'EURONEXT','point_type': 'exchange', 'name': 'Euronext Paris',                 'country': 'France',        'country_code': 'FR', 'latitude': 48.8698,  'longitude': 2.3384,   'metadata': {'timezone': 'Europe/Paris',    'website': 'euronext.com'}},
    {'code': 'DAX',    'point_type': 'exchange', 'name': 'Frankfurt Stock Exchange (Xetra)','country': 'Germany',       'country_code': 'DE', 'latitude': 50.1109,  'longitude': 8.6821,   'metadata': {'timezone': 'Europe/Berlin',   'website': 'deutsche-boerse.com'}},
    {'code': 'BSE',    'point_type': 'exchange', 'name': 'Bombay Stock Exchange',           'country': 'India',         'country_code': 'IN', 'latitude': 18.9322,  'longitude': 72.8347,  'metadata': {'timezone': 'Asia/Kolkata',    'website': 'bseindia.com'}},
    {'code': 'NSE_IN', 'point_type': 'exchange', 'name': 'National Stock Exchange of India','country': 'India',         'country_code': 'IN', 'latitude': 19.0607,  'longitude': 72.8362,  'metadata': {'timezone': 'Asia/Kolkata',    'website': 'nseindia.com'}},
    {'code': 'ASX',    'point_type': 'exchange', 'name': 'Australian Securities Exchange',  'country': 'Australia',     'country_code': 'AU', 'latitude': -33.8688, 'longitude': 151.2093, 'metadata': {'timezone': 'Australia/Sydney', 'website': 'asx.com.au'}},
    {'code': 'MOEX',   'point_type': 'exchange', 'name': 'Moscow Exchange',                 'country': 'Russia',        'country_code': 'RU', 'latitude': 55.7558,  'longitude': 37.6173,  'metadata': {'timezone': 'Europe/Moscow',   'website': 'moex.com'}},
    {'code': 'TADAWUL','point_type': 'exchange', 'name': 'Saudi Exchange (Tadawul)',         'country': 'Saudi Arabia',  'country_code': 'SA', 'latitude': 24.7136,  'longitude': 46.6753,  'metadata': {'timezone': 'Asia/Riyadh',    'website': 'saudiexchange.sa'}},
    {'code': 'JSE',    'point_type': 'exchange', 'name': 'Johannesburg Stock Exchange',     'country': 'South Africa',  'country_code': 'ZA', 'latitude': -26.2041, 'longitude': 28.0473,  'metadata': {'timezone': 'Africa/Johannesburg', 'website': 'jse.co.za'}},
    {'code': 'BOVESPA','point_type': 'exchange', 'name': 'B3 – São Paulo Stock Exchange',   'country': 'Brazil',        'country_code': 'BR', 'latitude': -23.5432, 'longitude': -46.6291, 'metadata': {'timezone': 'America/Sao_Paulo','website': 'b3.com.br'}},
    {'code': 'TSX',    'point_type': 'exchange', 'name': 'Toronto Stock Exchange',          'country': 'Canada',        'country_code': 'CA', 'latitude': 43.6482,  'longitude': -79.3833, 'metadata': {'timezone': 'America/Toronto',  'website': 'tsx.com'}},
    {'code': 'KRX',    'point_type': 'exchange', 'name': 'Korea Exchange',                  'country': 'South Korea',   'country_code': 'KR', 'latitude': 37.5668,  'longitude': 126.9780, 'metadata': {'timezone': 'Asia/Seoul',      'website': 'krx.co.kr'}},
    {'code': 'SGX',    'point_type': 'exchange', 'name': 'Singapore Exchange',              'country': 'Singapore',     'country_code': 'SG', 'latitude': 1.2897,   'longitude': 103.8501, 'metadata': {'timezone': 'Asia/Singapore',  'website': 'sgx.com'}},
    {'code': 'SIX',    'point_type': 'exchange', 'name': 'SIX Swiss Exchange',              'country': 'Switzerland',   'country_code': 'CH', 'latitude': 47.3769,  'longitude': 8.5417,   'metadata': {'timezone': 'Europe/Zurich',   'website': 'six-group.com'}},

    # ------------------------------------------------------------------ Commodity Exchanges
    {'code': 'CME',    'point_type': 'commodity_exchange', 'name': 'Chicago Mercantile Exchange', 'country': 'United States', 'country_code': 'US', 'latitude': 41.8810,  'longitude': -87.6323, 'metadata': {'products': ['futures', 'options', 'FX', 'rates']}},
    {'code': 'NYMEX',  'point_type': 'commodity_exchange', 'name': 'New York Mercantile Exchange','country': 'United States', 'country_code': 'US', 'latitude': 40.7580,  'longitude': -74.0142, 'metadata': {'products': ['crude oil', 'natural gas', 'gold', 'silver']}},
    {'code': 'CBOT',   'point_type': 'commodity_exchange', 'name': 'Chicago Board of Trade',      'country': 'United States', 'country_code': 'US', 'latitude': 41.8789,  'longitude': -87.6359, 'metadata': {'products': ['wheat', 'corn', 'soybeans', 'bonds']}},
    {'code': 'LME',    'point_type': 'commodity_exchange', 'name': 'London Metal Exchange',        'country': 'United Kingdom', 'country_code': 'GB', 'latitude': 51.5113, 'longitude': -0.0890,  'metadata': {'products': ['copper', 'aluminum', 'zinc', 'nickel']}},
    {'code': 'ICE',    'point_type': 'commodity_exchange', 'name': 'Intercontinental Exchange',    'country': 'United States', 'country_code': 'US', 'latitude': 33.7490,  'longitude': -84.3880, 'metadata': {'products': ['crude oil', 'natural gas', 'sugar', 'coffee']}},
    {'code': 'SHFE',   'point_type': 'commodity_exchange', 'name': 'Shanghai Futures Exchange',    'country': 'China',         'country_code': 'CN', 'latitude': 31.2244,  'longitude': 121.4694, 'metadata': {'products': ['copper', 'gold', 'crude oil', 'rubber']}},
    {'code': 'DCE',    'point_type': 'commodity_exchange', 'name': 'Dalian Commodity Exchange',    'country': 'China',         'country_code': 'CN', 'latitude': 38.9140,  'longitude': 121.6147, 'metadata': {'products': ['iron ore', 'soybean', 'palm oil']}},
    {'code': 'MCX',    'point_type': 'commodity_exchange', 'name': 'Multi Commodity Exchange India','country': 'India',        'country_code': 'IN', 'latitude': 19.0608,  'longitude': 72.8362,  'metadata': {'products': ['gold', 'silver', 'crude oil', 'natural gas']}},
    {'code': 'TOCOM',  'point_type': 'commodity_exchange', 'name': 'Tokyo Commodity Exchange',     'country': 'Japan',         'country_code': 'JP', 'latitude': 35.6840,  'longitude': 139.7692, 'metadata': {'products': ['rubber', 'platinum', 'gasoline']}},

    # ------------------------------------------------------------------ Major Ports
    {'code': 'PORT_SHANGHAI',   'point_type': 'port', 'name': 'Port of Shanghai',         'country': 'China',         'country_code': 'CN', 'latitude': 31.2304,  'longitude': 121.4737, 'metadata': {'type': 'container', 'teu_rank': 1}},
    {'code': 'PORT_SINGAPORE',  'point_type': 'port', 'name': 'Port of Singapore',        'country': 'Singapore',     'country_code': 'SG', 'latitude': 1.2897,   'longitude': 103.8198, 'metadata': {'type': 'container', 'teu_rank': 2}},
    {'code': 'PORT_NINGBO',     'point_type': 'port', 'name': 'Port of Ningbo-Zhoushan',  'country': 'China',         'country_code': 'CN', 'latitude': 29.8683,  'longitude': 121.5440, 'metadata': {'type': 'container', 'teu_rank': 3}},
    {'code': 'PORT_SHENZHEN',   'point_type': 'port', 'name': 'Port of Shenzhen',         'country': 'China',         'country_code': 'CN', 'latitude': 22.5431,  'longitude': 113.8968, 'metadata': {'type': 'container', 'teu_rank': 4}},
    {'code': 'PORT_GUANGZHOU',  'point_type': 'port', 'name': 'Port of Guangzhou',        'country': 'China',         'country_code': 'CN', 'latitude': 23.1291,  'longitude': 113.2644, 'metadata': {'type': 'container', 'teu_rank': 5}},
    {'code': 'PORT_BUSAN',      'point_type': 'port', 'name': 'Port of Busan',            'country': 'South Korea',   'country_code': 'KR', 'latitude': 35.0997,  'longitude': 129.0432, 'metadata': {'type': 'container', 'teu_rank': 6}},
    {'code': 'PORT_HONGKONG',   'point_type': 'port', 'name': 'Port of Hong Kong',        'country': 'Hong Kong',     'country_code': 'HK', 'latitude': 22.3193,  'longitude': 114.1694, 'metadata': {'type': 'container', 'teu_rank': 9}},
    {'code': 'PORT_ROTTERDAM',  'point_type': 'port', 'name': 'Port of Rotterdam',        'country': 'Netherlands',   'country_code': 'NL', 'latitude': 51.9244,  'longitude': 4.4777,   'metadata': {'type': 'container', 'teu_rank': 11, 'note': 'largest EU port'}},
    {'code': 'PORT_ANTWERP',    'point_type': 'port', 'name': 'Port of Antwerp-Bruges',   'country': 'Belgium',       'country_code': 'BE', 'latitude': 51.2990,  'longitude': 4.3906,   'metadata': {'type': 'container', 'teu_rank': 13}},
    {'code': 'PORT_HAMBURG',    'point_type': 'port', 'name': 'Port of Hamburg',          'country': 'Germany',       'country_code': 'DE', 'latitude': 53.5753,  'longitude': 9.9884,   'metadata': {'type': 'container', 'teu_rank': 17}},
    {'code': 'PORT_LA',         'point_type': 'port', 'name': 'Port of Los Angeles',      'country': 'United States', 'country_code': 'US', 'latitude': 33.7283,  'longitude': -118.2712,'metadata': {'type': 'container', 'note': 'busiest US port'}},
    {'code': 'PORT_NEWYORK',    'point_type': 'port', 'name': 'Port of New York & New Jersey','country': 'United States','country_code': 'US','latitude': 40.6652,  'longitude': -74.0714, 'metadata': {'type': 'container'}},
    {'code': 'PORT_DUBAI',      'point_type': 'port', 'name': 'Port of Jebel Ali (Dubai)','country': 'UAE',           'country_code': 'AE', 'latitude': 24.9992,  'longitude': 55.0573,  'metadata': {'type': 'container', 'teu_rank': 10}},
    {'code': 'PORT_SUEZ',       'point_type': 'port', 'name': 'Port Said (Suez Canal)',   'country': 'Egypt',         'country_code': 'EG', 'latitude': 31.2654,  'longitude': 32.3014,  'metadata': {'type': 'transit', 'note': 'Suez Canal northern entrance'}},
    {'code': 'PORT_HORMUZ',     'point_type': 'port', 'name': 'Strait of Hormuz (Bandar Abbas)','country': 'Iran',   'country_code': 'IR', 'latitude': 27.1832,  'longitude': 56.2666,  'metadata': {'type': 'transit', 'note': '20% global oil transit'}},
    {'code': 'PORT_MALACCA',    'point_type': 'port', 'name': 'Strait of Malacca (Port Klang)','country': 'Malaysia','country_code': 'MY', 'latitude': 3.0000,   'longitude': 101.4000, 'metadata': {'type': 'transit', 'note': '1/4 world trade passes here'}},

    # ------------------------------------------------------------------ Central Banks
    {'code': 'CB_FED',    'point_type': 'central_bank', 'name': 'US Federal Reserve',           'country': 'United States',  'country_code': 'US', 'latitude': 38.8979, 'longitude': -77.0455, 'metadata': {'currency': 'USD', 'website': 'federalreserve.gov'}},
    {'code': 'CB_ECB',    'point_type': 'central_bank', 'name': 'European Central Bank',         'country': 'Germany',        'country_code': 'DE', 'latitude': 50.1109, 'longitude': 8.7038,  'metadata': {'currency': 'EUR', 'website': 'ecb.europa.eu'}},
    {'code': 'CB_BOJ',    'point_type': 'central_bank', 'name': 'Bank of Japan',                 'country': 'Japan',          'country_code': 'JP', 'latitude': 35.6878, 'longitude': 139.7745, 'metadata': {'currency': 'JPY', 'website': 'boj.or.jp'}},
    {'code': 'CB_PBOC',   'point_type': 'central_bank', 'name': "People's Bank of China",        'country': 'China',          'country_code': 'CN', 'latitude': 39.9082, 'longitude': 116.3916, 'metadata': {'currency': 'CNY', 'website': 'pbc.gov.cn'}},
    {'code': 'CB_BOE',    'point_type': 'central_bank', 'name': 'Bank of England',               'country': 'United Kingdom', 'country_code': 'GB', 'latitude': 51.5144, 'longitude': -0.0891,  'metadata': {'currency': 'GBP', 'website': 'bankofengland.co.uk'}},
    {'code': 'CB_BOC',    'point_type': 'central_bank', 'name': 'Bank of Canada',                'country': 'Canada',         'country_code': 'CA', 'latitude': 45.4232, 'longitude': -75.7013, 'metadata': {'currency': 'CAD', 'website': 'bankofcanada.ca'}},
    {'code': 'CB_RBA',    'point_type': 'central_bank', 'name': 'Reserve Bank of Australia',     'country': 'Australia',      'country_code': 'AU', 'latitude': -33.8688,'longitude': 151.2093, 'metadata': {'currency': 'AUD', 'website': 'rba.gov.au'}},
    {'code': 'CB_SNB',    'point_type': 'central_bank', 'name': 'Swiss National Bank',           'country': 'Switzerland',    'country_code': 'CH', 'latitude': 46.9480, 'longitude': 7.4474,   'metadata': {'currency': 'CHF', 'website': 'snb.ch'}},
    {'code': 'CB_RBI',    'point_type': 'central_bank', 'name': 'Reserve Bank of India',         'country': 'India',          'country_code': 'IN', 'latitude': 18.9322, 'longitude': 72.8347,  'metadata': {'currency': 'INR', 'website': 'rbi.org.in'}},
    {'code': 'CB_SARB',   'point_type': 'central_bank', 'name': 'South African Reserve Bank',    'country': 'South Africa',   'country_code': 'ZA', 'latitude': -25.7479,'longitude': 28.2293,  'metadata': {'currency': 'ZAR', 'website': 'resbank.co.za'}},
    {'code': 'CB_BCB',    'point_type': 'central_bank', 'name': 'Banco Central do Brasil',       'country': 'Brazil',         'country_code': 'BR', 'latitude': -15.7801,'longitude': -47.9292, 'metadata': {'currency': 'BRL', 'website': 'bcb.gov.br'}},
    {'code': 'CB_CBR',    'point_type': 'central_bank', 'name': 'Central Bank of Russia',        'country': 'Russia',         'country_code': 'RU', 'latitude': 55.7617, 'longitude': 37.6195,  'metadata': {'currency': 'RUB', 'website': 'cbr.ru'}},
    {'code': 'CB_BOK',    'point_type': 'central_bank', 'name': 'Bank of Korea',                 'country': 'South Korea',    'country_code': 'KR', 'latitude': 37.4981, 'longitude': 126.9772, 'metadata': {'currency': 'KRW', 'website': 'bok.or.kr'}},
    {'code': 'CB_MAS',    'point_type': 'central_bank', 'name': 'Monetary Authority of Singapore','country': 'Singapore',     'country_code': 'SG', 'latitude': 1.2904,  'longitude': 103.8438, 'metadata': {'currency': 'SGD', 'website': 'mas.gov.sg'}},
    {'code': 'CB_SAMA',   'point_type': 'central_bank', 'name': 'Saudi Central Bank (SAMA)',     'country': 'Saudi Arabia',   'country_code': 'SA', 'latitude': 24.6748, 'longitude': 46.7082,  'metadata': {'currency': 'SAR', 'website': 'sama.gov.sa'}},
]


class Command(BaseCommand):
    help = 'Bootstrap static reference points (exchanges, ports, central banks)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--type',
            choices=['exchange', 'commodity_exchange', 'port', 'central_bank'],
            help='Only load a specific point type',
        )

    def handle(self, *args, **options):
        point_type_filter = options.get('type')
        points = STATIC_POINTS
        if point_type_filter:
            points = [p for p in points if p['point_type'] == point_type_filter]

        created = updated = 0
        for data in points:
            _, was_created = core_models.StaticPoint.objects.update_or_create(code=data['code'], defaults=data)
            if was_created:
                created += 1
            else:
                updated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Done: {created} created, {updated} updated ({len(points)} total)'
            )
        )
