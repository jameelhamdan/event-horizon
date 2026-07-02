"""Dependency-light self-tests for services/cache.py — the shared-Redis cache
key registry and get/set wrappers used by the pipeline dedup guards and the
LLM usage/debounce dashboard.

No real Redis required — cache_get/cache_set/get_redis_client are exercised
against a mocked django.core.cache.caches; the key builders are pure string
formatting.

Run standalone:
    DJANGO_SETTINGS_MODULE=settings.base python -m tests.tests_cache
"""

from unittest.mock import MagicMock, patch

from tests._runner import bootstrap_django, run

bootstrap_django()


# ── Key registry — pure string formatting, no I/O ─────────────────────────────

def test_key_backfill_checkpoint_shape():
    from services.cache import key_backfill_checkpoint
    key = key_backfill_checkpoint('2024-01-01', '2024-06-01')
    assert key == 'pipeline:backfill:v2:2024-01-01:2024-06-01:done'


def test_key_backfill_source_block_shape():
    from services.cache import key_backfill_source_block
    assert key_backfill_source_block('my-rss-feed') == 'pipeline:backfill:blocked:my-rss-feed'


def test_key_llm_cycle_shape():
    from services.cache import key_llm_cycle
    assert key_llm_cycle('groq', 'keys') == 'llm:cycle:groq:keys'
    assert key_llm_cycle('groq', 'models') == 'llm:cycle:groq:models'


def test_key_llm_debounce_shape():
    from services.cache import key_llm_debounce
    assert key_llm_debounce('cerebras', 'abc123') == 'llm:debounce:cerebras:abc123'


def test_key_llm_debounce_scan_pattern_is_a_glob():
    from services.cache import key_llm_debounce_scan_pattern
    assert key_llm_debounce_scan_pattern('cerebras') == 'llm:debounce:cerebras:*'


def test_key_llm_req_stat_shape():
    from services.cache import key_llm_req_stat
    assert key_llm_req_stat('openrouter', 'ok') == 'llm:req:openrouter:ok'
    assert key_llm_req_stat('openrouter', 'err') == 'llm:req:openrouter:err'


def test_key_llm_req_prefix_shape():
    from services.cache import key_llm_req_prefix
    assert key_llm_req_prefix('groq') == 'llm:req:groq'


def test_static_keys_are_namespaced():
    from services.cache import KEY_ARTICLE_TITLE_DEDUP, KEY_BOOTSTRAP_INITIAL_DATA_DONE
    assert KEY_ARTICLE_TITLE_DEDUP.startswith('pipeline:')
    assert KEY_BOOTSTRAP_INITIAL_DATA_DONE.startswith('pipeline:')


def test_all_key_builders_are_collision_free_by_prefix():
    """Every key-producing helper should live under a distinct namespace prefix
    so pipeline:* and llm:* keys can never collide with each other."""
    from services.cache import (
        KEY_ARTICLE_TITLE_DEDUP, KEY_BOOTSTRAP_INITIAL_DATA_DONE,
        key_backfill_checkpoint, key_backfill_source_block, key_llm_cycle, key_llm_debounce,
        key_llm_debounce_scan_pattern, key_llm_req_stat, key_llm_req_prefix,
    )
    sample_keys = [
        KEY_ARTICLE_TITLE_DEDUP,
        KEY_BOOTSTRAP_INITIAL_DATA_DONE,
        key_backfill_checkpoint('s', 'e'),
        key_backfill_source_block('src'),
        key_llm_cycle('p', 'keys'),
        key_llm_debounce('p', 'h'),
        key_llm_debounce_scan_pattern('p'),
        key_llm_req_stat('p', 'ok'),
        key_llm_req_prefix('p'),
    ]
    prefixes = {k.split(':', 1)[0] for k in sample_keys}
    assert prefixes == {'pipeline', 'llm'}


# ── cache_get / cache_set / get_redis_client — mocked backend ────────────────

def test_cache_set_and_get_use_redis_cache_backend():
    from services import cache as cache_mod

    fake_backend = MagicMock()
    fake_caches = {'redis-cache': fake_backend}

    with patch('django.core.cache.caches', fake_caches):
        cache_mod.cache_set('some:key', {'a': 1}, timeout=60)
        fake_backend.set.assert_called_once_with('some:key', {'a': 1}, timeout=60)

        fake_backend.get.return_value = {'a': 1}
        result = cache_mod.cache_get('some:key')

    assert result == {'a': 1}
    fake_backend.get.assert_called_once_with('some:key')


def test_blocklist_uses_redis_when_available():
    from services import cache as cache_mod

    fake_backend = MagicMock()
    fake_caches = {'redis-cache': fake_backend}
    bl = cache_mod.Blocklist()

    with patch('django.core.cache.caches', fake_caches):
        bl.block('some:key', ttl=30)
        fake_backend.set.assert_called_once_with('some:key', 1, timeout=30)

        fake_backend.get.return_value = 1
        assert bl.is_blocked('some:key') is True


def test_blocklist_falls_back_to_local_dict_when_redis_unavailable():
    from services import cache as cache_mod

    bl = cache_mod.Blocklist()
    broken_caches = MagicMock()
    broken_caches.__getitem__.side_effect = RuntimeError('no redis')

    with patch('django.core.cache.caches', broken_caches):
        bl.block('some:key', ttl=30)
        assert bl.is_blocked('some:key') is True
    assert bl.is_blocked('never:blocked') is False


def test_redis_set_add_and_members():
    from services import cache as cache_mod

    fake_client = MagicMock()
    fake_client.smembers.return_value = {b'a', b'b'}

    with patch.object(cache_mod, 'get_redis_client', return_value=fake_client):
        cache_mod.redis_set_add('some:set', 'a', ttl=60)
        fake_client.sadd.assert_called_once_with('some:set', 'a')
        fake_client.expire.assert_called_once_with('some:set', 60)

        members = cache_mod.redis_set_members('some:set')
    assert members == {'a', 'b'}


def test_get_redis_client_uses_write_flag():
    from services import cache as cache_mod

    fake_client = object()
    fake_backend = MagicMock()
    fake_backend.client.get_client.return_value = fake_client
    fake_caches = {'redis-cache': fake_backend}

    with patch('django.core.cache.caches', fake_caches):
        result = cache_mod.get_redis_client(write=False)

    fake_backend.client.get_client.assert_called_once_with(write=False)
    assert result is fake_client


# ── Runner ────────────────────────────────────────────────────────────────────

_TESTS = [
    test_key_backfill_checkpoint_shape,
    test_key_backfill_source_block_shape,
    test_key_llm_cycle_shape,
    test_key_llm_debounce_shape,
    test_key_llm_debounce_scan_pattern_is_a_glob,
    test_key_llm_req_stat_shape,
    test_key_llm_req_prefix_shape,
    test_static_keys_are_namespaced,
    test_all_key_builders_are_collision_free_by_prefix,
    test_cache_set_and_get_use_redis_cache_backend,
    test_blocklist_uses_redis_when_available,
    test_blocklist_falls_back_to_local_dict_when_redis_unavailable,
    test_redis_set_add_and_members,
    test_get_redis_client_uses_write_flag,
]


if __name__ == '__main__':
    run(_TESTS)
