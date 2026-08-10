# MIT License
"""Pure helpers that decide whether a collector state frame may hit disk.

Kept free of game APIs so the quit-vs-tick race can be regression-tested
outside the WoWS sandbox (Python 2 and 3).
"""


def should_write_state(collector_active, snap):
    """Return False when a late live tick would overwrite a quit frame.

    `_on_battle_quit` writes `active: false` then cancels the tick handle, but
    a tick already mid-build can still call submit afterwards. AsyncWriter is
    latest-wins, so that late live frame would erase the terminal inactive
    snapshot and the server would never see `ended`.
    """
    if not isinstance(snap, dict):
        return False
    if snap.get("active") and not collector_active:
        return False
    return True
