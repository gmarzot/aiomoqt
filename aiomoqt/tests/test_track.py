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

    def _pub_session(self, track_alias=0, request_id=1):
        """Mock session configured for PublishedTrack.publish() calls."""
        session = MagicMock()
        session.publish_namespace = AsyncMock(return_value=MagicMock())
        pub_response = MagicMock()
        pub_response.track_alias = track_alias
        pub_response.request_id = request_id
        session.publish = MagicMock(return_value=pub_response)
        session.register_handler = MagicMock()
        session.MOQT_D16_OVERRIDE_REGISTRY = {}
        return session

    # ----- Flow B: bare PUBLISH (default) -----

    def test_publish_flow_b_default_d14(self):
        """d14 default: Flow B — bare PUBLISH, no PUB_NS."""
        async def _test():
            session = self._pub_session()
            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.publish()
            session.publish_namespace.assert_not_called()
            session.publish.assert_called_once_with(
                namespace="bench", track_name="track", forward=0)
            assert t.state == TrackState.PUBLISHED
        asyncio.run(_test())

    def test_publish_flow_b_default_d16(self):
        """d16 default: Flow B — bare PUBLISH, no PUB_NS; REQUEST_UPDATE
        handler registered."""
        async def _test():
            session = self._pub_session(track_alias=42, request_id=7)
            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.publish()
            session.publish_namespace.assert_not_called()
            session.publish.assert_called_once()
            assert t.track_alias == 42
            assert t.request_id == 7
            assert t.state == TrackState.PUBLISHED
            assert 0x02 in session.MOQT_D16_OVERRIDE_REGISTRY
        asyncio.run(_test())

    # ----- Flow A: PUB_NS only -----

    def test_publish_flow_a_d14(self):
        """d14 Flow A: PUB_NS only, no PUBLISH; waits for SUBSCRIBE."""
        async def _test():
            session = self._pub_session()
            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.publish(announce_namespace=True,
                                publish_track=False)
            session.publish_namespace.assert_called_once()
            session.publish.assert_not_called()
            assert t.state == TrackState.ANNOUNCED
        asyncio.run(_test())

    def test_publish_flow_a_d16(self):
        """d16 Flow A: PUB_NS only, no PUBLISH."""
        async def _test():
            session = self._pub_session()
            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.publish(announce_namespace=True,
                                publish_track=False)
            session.publish_namespace.assert_called_once()
            session.publish.assert_not_called()
            assert t.state == TrackState.ANNOUNCED
        asyncio.run(_test())

    # ----- Hybrid: both PUB_NS and PUBLISH -----

    def test_publish_hybrid_d14(self):
        """d14 hybrid: PUB_NS + PUBLISH (legacy relays that want both)."""
        async def _test():
            session = self._pub_session()
            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.publish(announce_namespace=True,
                                publish_track=True)
            session.publish_namespace.assert_called_once()
            session.publish.assert_called_once()
            assert t.state == TrackState.PUBLISHED
        asyncio.run(_test())

    def test_publish_hybrid_d16(self):
        """d16 hybrid: PUB_NS + PUBLISH."""
        async def _test():
            session = self._pub_session()
            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.publish(announce_namespace=True,
                                publish_track=True)
            session.publish_namespace.assert_called_once()
            session.publish.assert_called_once()
            assert t.state == TrackState.PUBLISHED
        asyncio.run(_test())

    # ----- error cases -----

    def test_publish_both_false_raises(self):
        """publish() with both flags False is invalid."""
        async def _test():
            session = self._pub_session()
            t = PublishedTrack(session, "bench", "track")
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                try:
                    await t.publish(announce_namespace=False,
                                    publish_track=False)
                    assert False, "expected ValueError"
                except ValueError:
                    pass
            session.publish_namespace.assert_not_called()
            session.publish.assert_not_called()
        asyncio.run(_test())


class TestSubscribedTrack:
    """Tests for SubscribedTrack."""

    def test_init_auto_discover(self):
        session = MagicMock()
        t = SubscribedTrack(session, "bench")
        assert t.trackname is None  # auto-discover mode

    def test_init_explicit_trackname(self):
        session = MagicMock()
        t = SubscribedTrack(session, "bench", trackname="video")
        assert t.trackname == "video"

    def test_init_with_callback(self):
        session = MagicMock()
        cb = MagicMock()
        t = SubscribedTrack(session, "bench", on_object=cb)
        assert t.on_object is cb

    # ----- Pathway A: direct SUBSCRIBE (explicit trackname) -----

    def test_subscribe_d14_direct(self):
        """d14 + explicit trackname → direct SUBSCRIBE, no namespace."""
        async def _test():
            session = MagicMock()
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                return_value=MagicMock())
            session.subscribe = AsyncMock(return_value=MagicMock())
            session.on_object_received = None

            t = SubscribedTrack(session, "bench",
                                trackname="video", draft=14)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False):
                await t.subscribe()

            session.subscribe.assert_called_once()
            session.subscribe_namespace.assert_not_called()
            session.await_publish.assert_not_called()
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_direct(self):
        """d16 + explicit trackname → direct SUBSCRIBE, no namespace."""
        async def _test():
            session = MagicMock()
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                return_value=MagicMock())
            session.subscribe = AsyncMock(return_value=MagicMock())
            session.on_object_received = None

            t = SubscribedTrack(session, "bench",
                                trackname="video", draft=16)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                await t.subscribe()

            session.subscribe.assert_called_once()
            session.subscribe_namespace.assert_not_called()
            session.await_publish.assert_not_called()
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    # ----- Pathway B: namespace discovery (trackname=None) -----

    def test_subscribe_d14_discover(self):
        """d14 + trackname=None → subscribe_namespace, learn from PUBLISH."""
        async def _test():
            session = MagicMock()
            pub_msg = MagicMock(spec=[])
            pub_msg.track_namespace = (b"bench",)
            pub_msg.track_name = b"discovered-d14"
            pub_msg.request_id = 3
            pub_msg.forward = 0
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(return_value=pub_msg)
            session.send_control_message = MagicMock()
            session.on_object_received = None

            t = SubscribedTrack(session, "bench", draft=14)
            assert t.trackname is None
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=False), \
                 patch('aiomoqt.track.PublishOk') as mock_ok:
                mock_ok.return_value.serialize.return_value = MagicMock(data=b'')
                await t.subscribe()

            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            assert t.trackname == "discovered-d14"
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_d16_discover(self):
        """d16 + trackname=None → subscribe_namespace, learn from PUBLISH."""
        async def _test():
            session = MagicMock()
            pub_msg = MagicMock(spec=[])
            pub_msg.track_namespace = (b"bench",)
            pub_msg.track_name = b"discovered-d16"
            pub_msg.request_id = 10
            pub_msg.forward = 0
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(return_value=pub_msg)
            session.send_control_message = MagicMock()
            session.on_object_received = None

            t = SubscribedTrack(session, "bench", draft=16)
            assert t.trackname is None
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True), \
                 patch('aiomoqt.track.PublishOk') as mock_ok:
                mock_ok.return_value.serialize.return_value = MagicMock(data=b'')
                await t.subscribe()

            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            assert t.trackname == "discovered-d16"
            assert t.state == TrackState.SUBSCRIBED

        asyncio.run(_test())

    def test_subscribe_discover_timeout_raises(self):
        """Discovery timeout raises, no silent fallback to direct."""
        async def _test():
            session = MagicMock()
            session.subscribe_namespace = AsyncMock(
                return_value=MagicMock())
            session.await_publish = AsyncMock(
                side_effect=asyncio.TimeoutError())
            session.subscribe = AsyncMock(return_value=MagicMock())
            session.on_object_received = None

            t = SubscribedTrack(session, "bench", draft=16)
            with patch('aiomoqt.track.is_draft16_or_later',
                        return_value=True):
                try:
                    await t.subscribe()
                    assert False, "expected TimeoutError"
                except asyncio.TimeoutError:
                    pass

            session.subscribe_namespace.assert_called_once()
            session.await_publish.assert_called_once()
            session.subscribe.assert_not_called()

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
