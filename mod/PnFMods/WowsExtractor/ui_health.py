# MIT License
"""Read live HP from the dataHub avatar `health` component.

TTaroTeamPanel / Autospy bind Unbound to the same component:

    healthComponent.value      current HP
    healthComponent.max        max HP
    healthComponent.isAlive

`battle.getAllShips().health` is empty on some client builds; this UI-layer
component is populated for the whole roster, spotted or not.

Kept free of game APIs so the join rules can be tested outside the sandbox
(Python 2 and 3).
"""


def _comp_get(obj, name):
    if obj is None:
        return None
    try:
        return getattr(obj, name)
    except Exception:
        pass
    try:
        return obj[name]
    except Exception:
        return None


def _entity_comp(entity, key):
    if entity is None or key is None:
        return None
    try:
        return entity[key]
    except Exception:
        return None


def _is_alive_flag(value):
    if value is False or value == 0:
        return False
    if value is True or value == 1:
        return True
    return None


def _as_number(value):
    if value is None or value is False:
        return None
    if value is True:
        return None
    try:
        return float(value)
    except Exception:
        return None


def normalize_ui_health(value, maximum, is_alive):
    """Return {health, maxHealth} or None.

    Unbound treats value==0 while still alive as "bar not yet ticked" and
    draws a full bar. Copying raw 0 would mark the ship dead in the collector.
    That sentinel is marked ``untickedFull`` so unspotted (灭点) rows can
    refuse it: a dark bar sitting at 0 is not full HP.
    """
    alive = _is_alive_flag(is_alive)
    max_hp = _as_number(maximum)
    cur = _as_number(value)
    if alive is False:
        return {"health": 0, "maxHealth": max_hp, "alive": False}
    if max_hp is None and cur is None:
        return None
    if alive is True and (cur is None or cur <= 0):
        if max_hp is None or max_hp <= 0:
            return None
        return {"health": max_hp, "maxHealth": max_hp, "untickedFull": True}
    if cur is None:
        return None if max_hp is None else {"health": None, "maxHealth": max_hp}
    rec = {"health": cur}
    rec["maxHealth"] = max_hp
    return rec


def _alias_ids(avatar, vehicle):
    ids = []
    for src, name in ((avatar, "playerId"), (avatar, "id"), (vehicle, "id")):
        value = _comp_get(src, name)
        if value is None or value in ids:
            continue
        ids.append(value)
    return ids


def index_avatar_health(entities, cc):
    """Map every avatar/vehicle id on an entity to its UI health record."""
    index = {}
    if not entities or cc is None:
        return index
    health_key = _comp_get(cc, "health")
    avatar_key = _comp_get(cc, "avatar")
    vehicle_key = _comp_get(cc, "vehicle")
    for entity in entities:
        health = _entity_comp(entity, health_key)
        rec = normalize_ui_health(
            _comp_get(health, "value"),
            _comp_get(health, "max"),
            _comp_get(health, "isAlive"),
        )
        if rec is None:
            continue
        avatar = _entity_comp(entity, avatar_key)
        vehicle = _entity_comp(entity, vehicle_key)
        for alias in _alias_ids(avatar, vehicle):
            index[alias] = rec
    return index


def apply_ui_health(entry, index, for_unspotted=False):
    """Fill missing health / maxHealth on a ship or self dict. Returns entry.

    ``for_unspotted``: last-seen / 灭点 rows. Do not copy the Unbound
    "value==0 means full bar" sentinel, and let UI ``isAlive=False``
    overwrite a ghost that was still flagged alive.
    """
    if not isinstance(entry, dict) or not index:
        return entry
    rec = None
    for key in ("playerId", "vehicleId", "uiId"):
        alias = entry.get(key)
        if alias is None:
            continue
        rec = index.get(alias)
        if rec is not None:
            break
    if rec is None:
        return entry
    if rec.get("alive") is False:
        entry["alive"] = False
    copy_health = rec.get("health") is not None
    if for_unspotted and rec.get("untickedFull"):
        copy_health = False
    if entry.get("health") is None and copy_health:
        entry["health"] = rec["health"]
    if entry.get("maxHealth") is None and rec.get("maxHealth") is not None:
        entry["maxHealth"] = rec["maxHealth"]
    return entry
