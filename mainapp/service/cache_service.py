import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotModified

try:
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover - defensive when redis client is absent
    RedisError = ()

logger = logging.getLogger(__name__)

DOMAIN_CATALOG = 'catalog'
DOMAIN_RECORDS = 'records'
DOMAIN_LOOKUP_RECORDS = 'lookup_records'
DOMAIN_MEMBERS = 'members'
DOMAIN_ROLES = 'roles'

ALL_WORKSPACE_DOMAINS: tuple[str, ...] = (
    DOMAIN_CATALOG,
    DOMAIN_RECORDS,
    DOMAIN_LOOKUP_RECORDS,
    DOMAIN_MEMBERS,
    DOMAIN_ROLES,
)

STATIC_CATALOG_CACHE_KEY = 'static:catalog:business_types:v2'

_CACHE_TRANSPORT_ERRORS = (
    (RedisError, OSError, TimeoutError, ConnectionError, ValueError)
    if isinstance(RedisError, type)
    else (OSError, TimeoutError, ConnectionError, ValueError)
)
_ETAG_TOKEN_RE = re.compile(r'(?:W/)?"[^"]*"|\*')


def _safe_cache_get_checked(key: str) -> tuple[Any, bool]:
    try:
        return cache.get(key), True
    except _CACHE_TRANSPORT_ERRORS as exc:
        logger.warning('Cache get(%s) failed; degrading to database: %s', key, exc)
        return None, False


def _safe_cache_get_many_checked(keys: list[str]) -> tuple[dict[str, Any], bool]:
    if not keys:
        return {}, True
    try:
        return cache.get_many(keys), True
    except _CACHE_TRANSPORT_ERRORS as exc:
        logger.warning('Cache get_many(%d keys) failed; degrading to database: %s', len(keys), exc)
        return {}, False


def safe_cache_get(key: str, default: Any = None) -> Any:
    value, ok = _safe_cache_get_checked(key)
    if not ok or value is None:
        return default
    return value


def safe_cache_set(key: str, value: Any, timeout: int | None = None) -> bool:
    try:
        cache.set(key, value, timeout=timeout)
        return True
    except _CACHE_TRANSPORT_ERRORS as exc:
        logger.warning('Cache set(%s) failed; continuing without caching: %s', key, exc)
        return False


def safe_cache_set_many(mapping: dict[str, Any], timeout: int | None = None) -> bool:
    if not mapping:
        return True
    try:
        cache.set_many(mapping, timeout=timeout)
        return True
    except _CACHE_TRANSPORT_ERRORS as exc:
        logger.warning(
            'Cache set_many(%d keys) failed; continuing without caching: %s',
            len(mapping),
            exc,
        )
        return False


def safe_cache_add(key: str, value: Any, timeout: int | None = None) -> bool:
    try:
        return bool(cache.add(key, value, timeout=timeout))
    except _CACHE_TRANSPORT_ERRORS as exc:
        logger.warning('Cache add(%s) failed; continuing without caching: %s', key, exc)
        return False


def safe_cache_delete(key: str) -> bool:
    try:
        cache.delete(key)
        return True
    except _CACHE_TRANSPORT_ERRORS as exc:
        logger.warning('Cache delete(%s) failed: %s', key, exc)
        return False


def _version_key(workspace_id: int, domain: str) -> str:
    return f'ws:{workspace_id}:ver:{domain}'


def _new_version_token() -> str:
    return uuid.uuid4().hex[:16]


def get_workspace_domain_versions(
    workspace_id: int,
    domains: Iterable[str],
) -> tuple[dict[str, str], bool]:
    """Fetch current per-workspace domain version tokens, minting fresh tokens on cold misses."""
    ordered_domains = tuple(dict.fromkeys(d for d in domains if d in ALL_WORKSPACE_DOMAINS))
    keys_by_domain = {d: _version_key(workspace_id, d) for d in ordered_domains}
    found, cache_ok = _safe_cache_get_many_checked(list(keys_by_domain.values()))

    versions: dict[str, str] = {}
    for domain, key in keys_by_domain.items():
        token = found.get(key)
        if not token:
            fresh = _new_version_token()
            if cache_ok:
                if safe_cache_add(key, fresh, timeout=None):
                    token = fresh
                else:
                    existing, retry_ok = _safe_cache_get_checked(key)
                    if not retry_ok:
                        cache_ok = False
                    token = existing or fresh
            else:
                token = fresh
        versions[domain] = str(token)
    return versions, cache_ok


def invalidate_workspace_cache(
    workspace_id: int | None,
    *,
    domains: Iterable[str] = ALL_WORKSPACE_DOMAINS,
) -> None:
    """Immediately bump workspace domain versions and re-bump on commit if inside a transaction."""
    if not workspace_id:
        return

    target_domains = tuple(dict.fromkeys(d for d in domains if d in ALL_WORKSPACE_DOMAINS))
    if not target_domains:
        return

    def _bump(domains_to_bump: tuple[str, ...]) -> None:
        updates = {
            _version_key(int(workspace_id), domain): _new_version_token()
            for domain in domains_to_bump
        }
        safe_cache_set_many(updates, timeout=None)

    _bump(target_domains)

    conn = transaction.get_connection()
    if conn.in_atomic_block:
        pending_map = getattr(conn, '_jojocrm_pending_cache_invalidations', None)
        if pending_map is None:
            pending_map = {}
            conn._jojocrm_pending_cache_invalidations = pending_map

            def _flush_on_commit() -> None:
                queued = getattr(conn, '_jojocrm_pending_cache_invalidations', None) or {}
                conn._jojocrm_pending_cache_invalidations = None
                updates = {}
                for ws_id, dom_set in queued.items():
                    for dom in dom_set:
                        updates[_version_key(int(ws_id), dom)] = _new_version_token()
                if updates:
                    safe_cache_set_many(updates, timeout=None)

            transaction.on_commit(_flush_on_commit)

        ws_domains = pending_map.setdefault(int(workspace_id), set())
        ws_domains.update(target_domains)


def serialize_json_bytes(payload: Any) -> bytes:
    """Serialize payload deterministically to UTF-8 JSON bytes using DjangoJSONEncoder."""
    return json.dumps(
        payload,
        cls=DjangoJSONEncoder,
        separators=(',', ':'),
    ).encode('utf-8')


def compute_etag(body_bytes: bytes, *, scope: str = 'public') -> str:
    """Compute a deterministic strong ETag scoped to public or tenant+caller context."""
    hasher = hashlib.sha256()
    hasher.update(scope.encode('utf-8'))
    hasher.update(b':')
    hasher.update(body_bytes)
    return f'"{hasher.hexdigest()[:32]}"'


def _normalize_opaque_tag(token: str) -> str:
    stripped = token.strip()
    if stripped.startswith('W/'):
        stripped = stripped[2:].strip()
    return stripped


def etag_matches(if_none_match: str | None, current_etag: str) -> bool:
    """Return True if If-None-Match matches current_etag per RFC 9110 weak comparison."""
    if not if_none_match or not current_etag:
        return False
    target = _normalize_opaque_tag(current_etag)
    for token in _ETAG_TOKEN_RE.findall(if_none_match):
        if token == '*':
            return True
        if _normalize_opaque_tag(token) == target:
            return True
    return False


def build_cached_json_response(
    request,
    *,
    body_text: str,
    etag: str,
    cache_control: str,
    vary: str = '',
    cache_hit: bool | None = None,
    status: int = 200,
) -> HttpResponse:
    """Build a JSON HttpResponse or 304 Not Modified with ETag, Cache-Control, Vary, and X-Cache."""
    if etag_matches(request.META.get('HTTP_IF_NONE_MATCH'), etag):
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(body_text, status=status, content_type='application/json')

    response['ETag'] = etag
    response['Cache-Control'] = cache_control
    if vary:
        response['Vary'] = vary
    if cache_hit is not None:
        response['X-Cache'] = 'HIT' if cache_hit else 'MISS'
    return response


def get_cached_business_type_catalog_bundle(loader: Callable[[], dict]) -> dict:
    """Return cached pre-serialized static catalog JSON text and ETag without re-serializing hits."""
    cached, cache_ok = _safe_cache_get_checked(STATIC_CATALOG_CACHE_KEY)
    if (
        cache_ok
        and isinstance(cached, dict)
        and isinstance(cached.get('body_text'), str)
        and isinstance(cached.get('etag'), str)
    ):
        return {
            'body_text': cached['body_text'],
            'etag': cached['etag'],
            'cache_hit': True,
        }

    payload = loader()
    body_bytes = serialize_json_bytes(payload)
    body_text = body_bytes.decode('utf-8')
    etag = compute_etag(body_bytes, scope='public:catalog:business_types')

    if cache_ok:
        ttl = getattr(settings, 'STATIC_CATALOG_CACHE_TTL', 86400)
        safe_cache_set(
            STATIC_CATALOG_CACHE_KEY,
            {'body_text': body_text, 'etag': etag},
            timeout=ttl,
        )

    return {
        'body_text': body_text,
        'etag': etag,
        'cache_hit': False,
    }


def build_caller_cache_scope(user, workspace) -> str:
    """Return a caller-isolated cache scope segment keyed by role/permissions and user identity."""
    from .role_service import compute_user_permissions_version, is_workspace_owner

    perm_version = compute_user_permissions_version(user, workspace)
    perm_hash = hashlib.sha256(perm_version.encode('utf-8')).hexdigest()[:16]
    if is_workspace_owner(user, workspace):
        return f'owner.{perm_hash}'
    user_id = getattr(user, 'id', 0) or 0
    return f'user.{user_id}.{perm_hash}'


def get_or_set_workspace_cache_bundle(
    workspace_id: int,
    *,
    domains: Iterable[str],
    key_suffix: str,
    scope: str,
    loader: Callable[[], Any],
    timeout: int | None = None,
) -> dict:
    """Return cached {payload, body_text, etag, cache_hit} scoped strictly to workspace and version."""
    ordered_domains = tuple(dict.fromkeys(d for d in domains if d in ALL_WORKSPACE_DOMAINS))
    versions, cache_ok = get_workspace_domain_versions(workspace_id, ordered_domains)
    version_segment = '.'.join(f'{d}_{versions[d]}' for d in ordered_domains)
    cache_key = f'ws:{workspace_id}:{version_segment}:{key_suffix}'

    if cache_ok:
        cached, get_ok = _safe_cache_get_checked(cache_key)
        if not get_ok:
            cache_ok = False
        elif (
            isinstance(cached, dict)
            and 'payload' in cached
            and isinstance(cached.get('body_text'), str)
            and isinstance(cached.get('etag'), str)
        ):
            return {
                'payload': cached['payload'],
                'body_text': cached['body_text'],
                'etag': cached['etag'],
                'cache_hit': True,
            }

    payload = loader()
    body_bytes = serialize_json_bytes(payload)
    body_text = body_bytes.decode('utf-8')
    etag = compute_etag(body_bytes, scope=f'ws:{workspace_id}:{scope}')

    if cache_ok:
        ttl = timeout if timeout is not None else getattr(settings, 'WORKSPACE_CACHE_TTL', 300)
        safe_cache_set(
            cache_key,
            {
                'payload': payload,
                'body_text': body_text,
                'etag': etag,
            },
            timeout=ttl,
        )

    return {
        'payload': payload,
        'body_text': body_text,
        'etag': etag,
        'cache_hit': False,
    }
