import asyncio
import typing

from redis import asyncio as redis

from .._base import Event
from .base import BroadcastBackend


class RedisBackend(BroadcastBackend):
    def __init__(self, url: str):
        self._conn = redis.Redis.from_url(url)
        self._pubsub = self._conn.pubsub()
        self._ready = asyncio.Event()
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._listener = asyncio.create_task(self._pubsub_listener())

    async def connect(self) -> None:
        await self._pubsub.connect()

    async def disconnect(self) -> None:
        await self._pubsub.aclose()
        await self._conn.aclose()
        self._listener.cancel()

    async def subscribe(self, channel: str) -> None:
        self._ready.set()
        await self._pubsub.subscribe(channel)

    async def unsubscribe(self, channel: str) -> None:
        await self._pubsub.unsubscribe(channel)

    async def publish(self, channel: str, message: typing.Any) -> None:
        await self._conn.publish(channel, message)

    async def next_published(self) -> Event:
        return await self._queue.get()

    async def _pubsub_listener(self) -> None:
        # redis-py does not listen to the pubsub connection if there are no channels subscribed
        # so we need to wait until the first channel is subscribed to start listening
        await self._ready.wait()
        async for message in self._pubsub.listen():
            if message["type"] == "message":
                event = Event(
                    channel=message["channel"].decode(),
                    message=message["data"].decode(),
                )
                await self._queue.put(event)


class RedisStreamBackend(BroadcastBackend):
    def __init__(self, url: str):
        self.conn_url = url.replace("redis-stream", "redis", 1)
        self.streams: typing.Dict = dict()
        self._ready = asyncio.Event()
        self._producer = redis.Redis.from_url(self.conn_url)
        self._consumer = redis.Redis.from_url(self.conn_url)

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        await self._producer.close()
        await self._consumer.close()

    async def subscribe(self, channel: str) -> None:
        try:
            info = await self._consumer.xinfo_stream(channel)
            last_id = info["last-generated-id"]
        except aioredis.exceptions.ResponseError:
            last_id = "0"
        self.streams[channel] = last_id
        self._ready.set()

    async def unsubscribe(self, channel: str) -> None:
        self.streams.pop(channel, None)

    async def publish(self, channel: str, message: typing.Any) -> None:
        await self._producer.xadd(channel, {"message": message})

    async def wait_for_messages(self) -> typing.List:
        await self._ready.wait()
        messages = None
        while not messages:
            messages = await self._consumer.xread(self.streams, count=1, block=1000)
        return messages

    async def next_published(self) -> Event:
        messages = await self.wait_for_messages()
        stream, events = messages[0]
        _msg_id, message = events[0]
        self.streams[stream.decode("utf-8")] = _msg_id.decode("utf-8")
        return Event(
            channel=stream.decode("utf-8"),
            message=message.get(b"message").decode("utf-8"),
        )