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
        t = Track(session, "live/sports", "video")
        assert t.fqtn == "live/sports/video"

    def test_fqtn_nested_namespace(self):
        session = MagicMock()
        t = Track(session, "live/sports/nfl", "video")
        assert t.fqtn == "live/sports/nfl/video"

    def test_repr(self):
        session = MagicMock()
        t = Track(session, "bench", "track")
        r = repr(t)
        assert "Track" in r
        assert "bench/track" in r
        assert "IDLE" in r

    def test_state_ordering(self):
        assert TrackState.IDLE < TrackState.ANNOUNCED
        assert TrackState.ANNOUNCED < TrackState.PUBLISHED
        assert TrackState.PUBLISHED < TrackState.SUBSCRIBED
        assert TrackState.SUBSCRIBED < TrackState.CLOSED


class TestPublishedTrack:
    """Tests for PublishedTrack."""

    def test_init(self):
        session = MagicMock()
        t = PublishedTrack(session, "bench", "track")
        assert t.priority == 255
        assert t.auth_token == b"bench-token"
        assert t._generating is False

    def test_init_custom_priority(self):
        session = MagicMock()
        t = PublishedTrack(session, "bench", "track", priority=128)
        assert t.priority == 128

    def test_init_custom_auth(self):
        session = MagicMock()
        t = PublishedTrack(session, "bench", "track",
                          auth_token=b"custom")
        assert t.auth_token == b"custom"

    def test_publish_d14(self):
        """d14: sends PUBLISH_NAMESPACE + PUBLISH(forward=0)."""
        async def _test():
            session = MagicMock()
            session.publish_namespace = AsyncMock(
                return_value=MagicMock())
            pub_response = MagicMock()
            pub_response.track_alias = 0
            pub_response.request_id = 1
            session.publish = MagicMock(return_value=pub_response)
            session.register_handler = MagicMock()

            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.publish()

            session.publish_namespace.assert_called_once()
            session.publish.assert_called_once_with(
                namespace="bench", track_name="track", forward=0)
            assert t.state == TrackState.PUBLISHED
            assert session.register_handler.call_count == 3

        asyncio.run(_test())

    def test_publish_d16(self):
        """d16: sends PUBLISH_NAMESPACE + PUBLISH(forward=0)
        + registers REQUEST_UPDATE handler."""
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
                namespace="bench", track_name="track", forward=0)
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
        assert t._auto_discover is True
        assert t.trackname == "track"  # default

    def test_init_explicit_trackname(self):
        session = MagicMock()
        t = SubscribedTrack(session, "bench", trackname="video")
        assert t._auto_discover is False
        assert t.trackname == "video"

    def test_init_with_callback(self):
        session = MagicMock()
        cb = MagicMock()
        t = SubscribedTrack(session, "bench", on_object=cb)
        assert t.on_object is cb

    def test_subscribe_d14(self):
        """d14 — discovery via subscribe_namespace + await_publish,
        fallback to direct subscribe if discovery fails."""
        async def _test():
            session = MagicMock()
            # Discovery fails (no PUBLISH from relay)
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                side_effect=asyncio.TimeoutError())
            session.subscribe = AsyncMock(
                return_value=MagicMock())
            session.on_object_received = None

            t = SubscribedTrack(session, "bench",
                                trackname="track", draft=14)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.subscribe()

            # Tried discovery first
            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            # Fell back to direct subscribe
            session.subscribe.assert_called_once()
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_explicit_track(self):
        """d16 — explicit trackname, PUBLISH discovery."""
        async def _test():
            session = MagicMock()
            pub_msg = MagicMock(spec=[])
            pub_msg.track_namespace = (b"bench",)
            pub_msg.track_name = b"video"
            pub_msg.request_id = 5
            pub_msg.forward = 0
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                return_value=pub_msg)
            session.send_control_message = MagicMock()
            session.on_object_received = None

            t = SubscribedTrack(session, "bench",
                                trackname="video", draft=16)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True), \
                 patch('aiomoqt.track.PublishOk') as mock_ok:
                mock_ok.return_value.serialize.return_value = MagicMock(data=b'')
                await t.subscribe()

            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            # Sent PUBLISH_OK
            session.send_control_message.assert_called_once()
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_auto_discover(self):
        """d16 — auto-discover trackname from PUBLISH."""
        async def _test():
            session = MagicMock()
            pub_msg = MagicMock(spec=[])
            pub_msg.track_namespace = (b"bench",)
            pub_msg.track_name = b"discovered-track"
            pub_msg.request_id = 10
            pub_msg.forward = 0
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                return_value=pub_msg)
            session.send_control_message = MagicMock()
            session.on_object_received = None

            t = SubscribedTrack(session, "bench", draft=16)
            assert t._auto_discover is True
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True), \
                 patch('aiomoqt.track.PublishOk') as mock_ok:
                mock_ok.return_value.serialize.return_value = MagicMock(data=b'')
                await t.subscribe()

            assert t.trackname == "discovered-track"
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_discover_timeout(self):
        """d16 — discovery timeout falls back to direct subscribe."""
        async def _test():
            session = MagicMock()
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                side_effect=asyncio.TimeoutError())
            session.subscribe = AsyncMock(
                return_value=MagicMock())
            session.on_object_received = None

            t = SubscribedTrack(session, "bench",
                                trackname="video", draft=16)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.subscribe()

            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            # Fell back to direct subscribe
            session.subscribe.assert_called_once()
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_on_object_callback_set(self):
        """on_object callback is wired to session."""
        async def _test():
            session = MagicMock()
            # Discovery succeeds
            pub_msg = MagicMock(spec=[])
            pub_msg.track_namespace = (b"bench",)
            pub_msg.track_name = b"track"
            pub_msg.request_id = 0
            pub_msg.forward = 0
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                return_value=pub_msg)
            session.send_control_message = MagicMock()
            session.subscribe = AsyncMock(return_value=MagicMock())
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
