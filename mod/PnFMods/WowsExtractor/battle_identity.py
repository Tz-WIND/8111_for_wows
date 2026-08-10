# MIT License
"""Python 2/3-compatible battle identity state, independent of the game API."""

try:
    text_type = unicode
except NameError:  # Python 3
    text_type = str


def _text(value):
    try:
        return text_type(value)
    except Exception:
        return "x"


def create_session_nonce():
    """Create one best-effort unique nonce for the lifetime of the mod load."""
    try:
        import uuid
        return uuid.uuid4().hex
    except Exception:
        pass

    try:
        import time
        stamp = int(time.time() * 1000000)
    except Exception:
        stamp = 0
    try:
        import random
        entropy = random.getrandbits(64)
    except Exception:
        entropy = id(object())
    return "%x-%x-%x" % (stamp, entropy, id(object()))


def normalize_arena_id(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == 0:
        return None
    text = _text(value).strip()
    if not text:
        return None
    if text.startswith("arena-"):
        return text
    return "arena-" + text


def make_fallback_battle_id(session_nonce, sequence, player_id):
    nonce = _text(session_nonce).strip()
    if not nonce:
        raise ValueError("session_nonce must not be empty")
    player = "x" if player_id is None else _text(player_id)
    return "session-%s-b%s-p%s" % (nonce, int(sequence), player)


class BattleIdentity(object):
    """Own the monotonic fallback ID and optional late arena promotion."""

    def __init__(self, session_nonce):
        nonce = _text(session_nonce).strip()
        if not nonce:
            raise ValueError("session_nonce must not be empty")
        self.session_nonce = nonce
        self.sequence = 0
        self.current = None
        self.is_arena = False

    def start(self, player_id=None, arena_id=None):
        self.sequence += 1
        self.current = make_fallback_battle_id(
            self.session_nonce, self.sequence, player_id)
        self.is_arena = False
        return self.promote(arena_id)

    def promote(self, arena_id):
        promoted = normalize_arena_id(arena_id)
        if promoted is not None:
            self.current = promoted
            self.is_arena = True
        return self.current
