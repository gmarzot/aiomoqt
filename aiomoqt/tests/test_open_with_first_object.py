"""A new subgroup stream opens with its header and first object in one
open_uni_stream call, with the track's stream priority; later objects are
plain writes."""
import pytest

from aiomoqt.context import profile_for
from aiomoqt.delivery import StreamMapping, SubgroupDelivery


class _Session:
    _profile = profile_for(18)

    def __init__(self):
        self.opens, self.writes, self.fins = [], [], []
        self._next = 2

    async def open_uni_stream(self, initial=b"", priority=None):
        self._next += 4
        self.opens.append((self._next, bytes(initial), priority))
        return self._next

    def set_stream_priority(self, *_):
        return True

    async def stream_write_drain(self, sid, data, end_stream=False):
        self.writes.append((sid, bytes(data)))

    def stream_write(self, sid, data, end_stream=False):
        if end_stream:
            self.fins.append(sid)

    def stream_reset(self, sid, code):
        pass


@pytest.mark.parametrize("mapping", [StreamMapping.PER_GROUP,
                                     StreamMapping.PER_OBJECT])
async def test_header_and_first_object_open_the_stream(mapping):
    session = _Session()
    delivery = SubgroupDelivery(session, 7, mapping=mapping, stream_priority=40)
    assert await delivery.write(0, 0, b"first", group_start=True)
    assert await delivery.write(0, 1, b"second")
    if mapping is StreamMapping.PER_GROUP:
        assert len(session.opens) == 1
        sid, initial, priority = session.opens[0]
        assert priority == 40 and initial.endswith(b"first")
        assert session.writes == [(sid, session.writes[0][1])]
        assert session.writes[0][1].endswith(b"second")
    else:
        assert [o[2] for o in session.opens] == [40, 40]
        assert [o[1][-5:] for o in session.opens] == [b"first", b"econd"]
        assert session.writes == []
        assert session.fins == [o[0] for o in session.opens]
