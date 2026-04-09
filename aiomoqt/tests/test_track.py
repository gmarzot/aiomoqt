"""Unit tests for aiomoqt.track — Track, PublishedTrack, SubscribedTrack."""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from aiomoqt.track import Track, PublishedTrack, SubscribedTrack, TrackState
from aiomoqt.context import set_moqt_ctx_version


class TestTrackBase:
    """Tests for Track base class."""

    def test_init_defaults(self):
        session = MagicMock()
        t = Track(session, "live/test", "video")
        assert t.namespace == "live/test"
        assert t.trackname == "video"
        assert t.object_size == 1024
        assert t.group_size == 60
        assert t.num_subgroups == 1
        assert t.rate == 0
        assert t.state == TrackState.IDLE
        assert t.track_alias == 0

    def test_init_custom(self):
        session = MagicMock()
        t = Track(session, "bench", "500k-track",
                  object_size=500000, group_size=30,
                  num_subgroups=4, rate=30)
        assert t.object_size == 500000
        assert t.group_size == 30
        assert t.num_subgroups == 4
        assert t.rate == 30

    def test_fqtn(self):
        session = MagicMock()
        t = Track(session, "bench", "track")
        assert t.fqtn == "bench/track"

    def test_fqtn_nested_namespace(self):
        session = MagicMock()
        t = Track(session, "live/sports/nfl", "video-4k")
        assert t.fqtn == "live/sports/nfl/video-4k"

    def test_repr(self):
        session = MagicMock()
        t = Track(session, "bench", "track")
        assert "Track(bench/track" in repr(t)
        assert "IDLE" in repr(t)

    def test_state_ordering(self):
        assert TrackState.IDLE < TrackState.ANNOUNCED
        assert TrackState.ANNOUNCED < TrackState.PUBLISHED
        assert TrackState.PUBLISHED < TrackState.SUBSCRIBED
        assert TrackState.SUBSCRIBED < TrackState.CLOSED


class TestPublishedTrack:
    """Tests for PublishedTrack."""

    def test_init(self):
        session = MagicMock()
        t = PublishedTrack(session, "bench", "track",
                           object_size=500000, rate=30)
        assert t.namespace == "bench"
        assert t.trackname == "track"
        assert t.object_size == 500000
        assert t.rate == 30
        assert t.priority == 255
        assert t.state == TrackState.IDLE
        assert t.auth_token == b"bench-token"

    def test_init_custom_priority(self):
        session = MagicMock()
        t = PublishedTrack(session, "bench", "track", priority=128)
        assert t.priority == 128

    def test_init_custom_auth(self):
        session = MagicMock()
        t = PublishedTrack(session, "bench", "track",
                           auth_token=b"my-token")
        assert t.auth_token == b"my-token"

    def test_publish_d14(self):
        """Test publish() for draft-14 (no PUBLISH message)."""
        async def _test():
            session = MagicMock()
            session.publish_namespace = AsyncMock(
                return_value=MagicMock())
            session.register_handler = MagicMock()

            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.publish()

            session.publish_namespace.assert_called_once()
            assert t.state == TrackState.ANNOUNCED
            session.register_handler.assert_called_once()

        asyncio.run(_test())

    def test_publish_d16(self):
        """Test publish() for draft-16 (sends PUBLISH message)."""
        async def _test():
            session = MagicMock()
            session.publish_namespace = AsyncMock(
                return_value=MagicMock())
            pub_response = MagicMock()
            pub_response.track_alias = 42
            pub_response.request_id = 7
            session.publish = MagicMock(return_value=pub_response)
            session.register_handler = MagicMock()
            session.MOQT_D16_OVERRIDE_REGISTRY = {}

            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.publish()

            session.publish_namespace.assert_called_once()
            session.publish.assert_called_once_with(
                namespace="bench", track_name="track")
            assert t.track_alias == 42
            assert t.request_id == 7
            assert t.state == TrackState.PUBLISHED
            assert 0x02 in session.MOQT_D16_OVERRIDE_REGISTRY

        asyncio.run(_test())


class TestSubscribedTrack:
    """Tests for SubscribedTrack."""

    def test_init_auto_discover(self):
        session = MagicMock()
        t = SubscribedTrack(session, "bench")
        assert t.namespace == "bench"
        assert t.trackname == "track"
        assert t._auto_discover is True
        assert t.state == TrackState.IDLE

    def test_init_explicit_trackname(self):
        session = MagicMock()
        t = SubscribedTrack(session, "bench", trackname="my-track")
        assert t.trackname == "my-track"
        assert t._auto_discover is False

    def test_init_with_callback(self):
        session = MagicMock()
        cb = MagicMock()
        t = SubscribedTrack(session, "bench", on_object=cb)
        assert t.on_object is cb

    def test_subscribe_d14(self):
        """d14 — direct subscribe, no namespace subscription."""
        async def _test():
            session = MagicMock()
            session.subscribe = AsyncMock(
                return_value=MagicMock())
            session.on_object_received = None

            t = SubscribedTrack(session, "bench",
                                trackname="track", draft=14)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.subscribe()

            session.subscribe.assert_called_once()
            kw = session.subscribe.call_args[1]
            assert kw['namespace'] == "bench"
            assert kw['track_name'] == "track"
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_explicit_track(self):
        """d16 explicit trackname — PUBLISH_OK subscribes."""
        async def _test():
            set_moqt_ctx_version(16)
            session = MagicMock()
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.send_control_message = MagicMock()

            pub_msg = MagicMock()
            pub_msg.request_id = 1
            pub_msg.track_namespace = (b'bench',)
            pub_msg.track_name = b'my-track'
            session.await_publish = AsyncMock(
                return_value=pub_msg)

            t = SubscribedTrack(session, "bench",
                                trackname="my-track", draft=16)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.subscribe()

            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            # PUBLISH_OK sent, no explicit subscribe
            session.send_control_message.assert_called_once()
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_auto_discover(self):
        """d16 auto-discovery: PUBLISH_OK(forward=1) subscribes."""
        async def _test():
            set_moqt_ctx_version(16)
            session = MagicMock()
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.send_control_message = MagicMock()
            session.on_object_received = None

            pub_msg = MagicMock()
            pub_msg.request_id = 1
            pub_msg.track_namespace = (b'bench',)
            pub_msg.track_name = b'500k-30fps-x1-abc4'
            session.await_publish = AsyncMock(
                return_value=pub_msg)

            t = SubscribedTrack(session, "bench", draft=16)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.subscribe()

            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            assert t.namespace == "bench"
            assert t.trackname == "500k-30fps-x1-abc4"
            # PUBLISH_OK sent via send_control_message
            session.send_control_message.assert_called_once()
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_discover_timeout(self):
        """d16 discovery timeout raises TimeoutError."""
        async def _test():
            session = MagicMock()
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                side_effect=TimeoutError)

            t = SubscribedTrack(session, "bench", draft=16)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                with pytest.raises(TimeoutError):
                    await t.subscribe(timeout=0.1)

        asyncio.run(_test())

    def test_on_object_callback_set(self):
        """on_object callback is wired to session."""
        async def _test():
            session = MagicMock()
            session.subscribe = AsyncMock(
                return_value=MagicMock())
            session.on_object_received = None

            cb = MagicMock()
            t = SubscribedTrack(session, "bench",
                                trackname="track",
                                on_object=cb, draft=14)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.subscribe()

            assert session.on_object_received is cb

        asyncio.run(_test())
