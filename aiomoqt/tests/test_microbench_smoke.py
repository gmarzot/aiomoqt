"""The microbenchmarks run non-gating in CI, so a codec mismatch in
their parse loop shows here instead: B1 decodes every draft's corpus."""
import pytest
from aiopquic.streamchain import StreamChain

from aiomoqt.context import profile_for
from aiomoqt.tests.microbench._bytestream import chunked, make_subgroup_stream
from aiomoqt.tests.microbench.b1_parser import _parse_subgroup_stream

_N_OBJECTS = 20


@pytest.mark.parametrize("draft", [14, 16, 18])
@pytest.mark.parametrize("extensions", [False, True])
@pytest.mark.parametrize("chunk_size", [0, 1500])
def test_b1_parses_every_object(draft, extensions, chunk_size):
    data = make_subgroup_stream(_N_OBJECTS, 4096, extensions=extensions,
                                draft=draft)
    chain = StreamChain()
    for chunk in chunked(data, chunk_size) if chunk_size else [data]:
        chain.extend(chunk)
    assert _parse_subgroup_stream(chain, prof=profile_for(draft)) == _N_OBJECTS
