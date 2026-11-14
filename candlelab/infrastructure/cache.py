import json
import logging

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class CandleCache:
    def __init__(self, settings, client=None):
        self.client = (
            client
            if client is not None
            else Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                decode_responses=True,
            )
        )
        self.ttl = settings.cache_ttl

    def read(self, key, loader):
        try:
            value = self.client.get(key)
            if value:
                result = json.loads(value)
                if (
                    isinstance(result, dict)
                    and isinstance(result.get("items"), list)
                    and "next_cursor" in result
                ):
                    return result
        except (RedisError, ValueError, TypeError):
            logger.debug("Cache indisponível ou inválido; lendo MongoDB.")
        result = loader()
        try:
            self.client.setex(key, self.ttl, json.dumps(result))
        except RedisError:
            logger.debug("Cache indisponível; leitura atendida pelo MongoDB.")
        return result
