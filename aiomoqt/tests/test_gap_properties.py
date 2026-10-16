"""pub_bench --gaps publishes skipped Group and Object IDs, each Object
naming its gap once (§12.8, §12.9)."""
import pytest

from aiomoqt.server import MOQTServer
from aiomoqt.types import MOQTMessageType

from aiomoqt.tests._certs import CERT, KEY, requires_certs

pytestmark = requires_certs

PRIOR_GROUP_ID_GAP = 0x3C
PRIOR_OBJECT_ID_GAP = 0x3E
IMMUTABLE_PROPERTIES = 0x0B


@pytest.mark.asyncio
@pytest.mark.parametrize("use_quic", [True, False], ids=["quic", "wt"])
async def test_pub_bench_publishes_gaps(use_quic, monkeypatch):
    from aiomoqt.tools import pub_bench
    port = 16580 + (0 if use_quic else 1)
    objects = []

    async def on_publish(session, msg):
        def on_object(obj, _size, _now, group_id, *_):
            if obj.payload:
                objects.append((group_id, obj.object_id,
                                dict(obj.extensions or {})))
        session.register_object_handler(msg.track_alias, on_object)
        session.publish_ok(msg, forward=1)

    server = MOQTServer(host="localhost", port=port, certificate=CERT,
                        private_key=KEY, path="/", use_quic=use_quic,
                        supported_drafts=18)
    server.register_handler(MOQTMessageType.PUBLISH, on_publish)
    quic_server = await server.serve()
    url = (f"moqt://localhost:{port}" if use_quic
           else f"https://localhost:{port}/")
    monkeypatch.setattr("sys.argv", [
        "pub_bench", url, "-k", "--draft", "18", "-N", "gap", "-T", "t",
        "--gaps", "-g", "3", "-r", "200", "-t", "1", "--no-stats"])
    try:
        await pub_bench.run(pub_bench.parse_args())
    finally:
        quic_server.close()

    groups = sorted({g for g, _, _ in objects})
    assert len(groups) >= 3, objects
    assert all(g % 2 == 0 for g in groups)
    seen_group_gap = seen_object_gap = False
    for group_id, object_id, props in objects:
        assert object_id % 2 == 0
        assert IMMUTABLE_PROPERTIES not in props
        if group_id > 0 and object_id == 0:
            assert props.get(PRIOR_GROUP_ID_GAP) == 1
            seen_group_gap = True
        else:
            assert PRIOR_GROUP_ID_GAP not in props
        if object_id > 0:
            assert props.get(PRIOR_OBJECT_ID_GAP) == 1
            seen_object_gap = True
        else:
            assert PRIOR_OBJECT_ID_GAP not in props
    assert seen_group_gap and seen_object_gap
