"""draft-20 message shapes, shared by d20 and d22.

SETUP keeps the d18 frame (type 0x2F00, 16-bit Length, delta-coded Setup
Options) and adds the d20 Setup Options registry. PUBLISH_STATE_NOTIFY is
new in d20.
"""
from .publish import PublishStateNotify
from .session_setup import (
    OPTION_VALUE_MAX, REPEATABLE_OPTIONS, SETUP_LENGTH_MAX, SETUP_OPTIONS,
    Setup,
)

__all__ = ['Setup', 'PublishStateNotify', 'SETUP_OPTIONS',
           'REPEATABLE_OPTIONS', 'SETUP_LENGTH_MAX', 'OPTION_VALUE_MAX']
