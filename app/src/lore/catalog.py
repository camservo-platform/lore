"""Races and classes players pick from, and the abilities each gives.

The core catalog below works the same in every world, but each forged world gives it
its own names and descriptions (a "theme": a warrior in one world is a gunner in
another), so it fits the setting while the numbers and limits stay balanced. A world
can also add a few options of its own (stored per campaign, same shape). When a
character is made, its abilities are copied onto it, so later catalog edits never
change a character in play.

How often abilities can be used is decided per class:
- resource "uses": each ability has its own number of uses (None means unlimited).
- resource "pool": abilities share one pool of points (named per class) and each has a
  cost (0 means free).
- refresh "recovery": uses and points come back when the Game Master calls `recover`
  (a safe night's sleep, a proper camp); "session": they come back at the start of each
  play session.
A race's abilities always have their own uses and come back on recovery.

Option shapes:
  class: {kind, name, summary, description, hp, defense, attributes, resource, abilities}
  race:  {kind, name, summary, description, hp, attributes, abilities}
  resource: {kind: "uses"} or {kind: "pool", name, base, per_level}, plus refresh
  ability: {name, summary, description, effect, level, uses, cost}
"""

import re
from collections import Counter
from typing import Any

REFRESHES = ("recovery", "session")
KINDS = ("race", "class")


def ability(name: str, summary: str, description: str, effect: str, *, level: int = 1,
            uses: int | None = None, cost: int | None = None) -> dict[str, Any]:
    return {"key": key_of(name), "name": name, "summary": summary, "description": description, "effect": effect,
            "level": level, "uses": uses, "cost": cost}


def key_of(name: str) -> str:
    """A stable id for a core option or ability, whatever a world calls it."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


CLASSES: list[dict[str, Any]] = [
    {
        "kind": "class", "name": "Warden",
        "summary": "A stalwart protector who stands between friends and harm.",
        "description": "Wardens train to hold a line, guard a gate or carry a wounded friend out of the fray. "
                       "Tough and steady, they are hardest to hit and keep the party standing.",
        "hp": 12, "defense": 14, "attributes": {"strength": 14, "agility": 10, "wits": 10, "presence": 12},
        "resource": {"kind": "uses", "refresh": "recovery"},
        "abilities": [
            ability("Guard", "Take a blow meant for a friend.",
                    "You step in front of an ally beside you and turn the attack onto yourself.",
                    "Until your next turn, the next attack against an adjacent ally targets you instead."),
            ability("Shield Wall", "Lock shields to harden the line.",
                    "You set your feet and draw nearby allies in behind your guard.",
                    "Until your next turn, you and allies beside you get +2 defense.", uses=2),
            ability("Rallying Shout", "A battle cry that steadies the party.",
                    "Your voice cuts through the chaos and puts heart back into your companions.",
                    "Every ally who can hear you gains 1d6 temporary HP.", uses=2),
            ability("Unbreakable", "Refuse to fall.",
                    "When a blow should drop you, sheer will keeps you on your feet.",
                    "When you would drop to 0 HP, you drop to 1 HP instead.", level=3, uses=1),
        ],
    },
    {
        "kind": "class", "name": "Vanguard",
        "summary": "A fighter who builds momentum and spends it on daring moves.",
        "description": "Vanguards are soldiers, duelists and sellswords who read a fight as it unfolds. "
                       "They draw on a pool of Grit for bursts of skill: counters, flurries and charges.",
        "hp": 11, "defense": 13, "attributes": {"strength": 13, "agility": 13, "wits": 10, "presence": 10},
        "resource": {"kind": "pool", "name": "Grit", "base": 3, "per_level": 1, "refresh": "recovery"},
        "abilities": [
            ability("Measured Strike", "Study a foe before you swing.",
                    "You spend a moment reading your opponent's stance before committing.",
                    "+1 to your next attack roll against a foe you spent your move watching.", cost=0),
            ability("Riposte", "Turn a miss into an opening.",
                    "The moment a blow goes wide, your blade answers.",
                    "When an enemy misses you in melee, make one attack against it at once.", cost=1),
            ability("Flurry", "A rapid burst of blows.",
                    "You press the attack with everything you have.",
                    "Make two attacks this turn instead of one.", cost=2),
            ability("Unstoppable Charge", "Crash through a foe.",
                    "You rush a foe with enough force to bowl them over.",
                    "Move and attack; on a hit, deal an extra 2d6 damage and knock the foe down.",
                    level=3, cost=3),
        ],
    },
    {
        "kind": "class", "name": "Arcanist",
        "summary": "A scholar of magic who shapes raw Aether into spells.",
        "description": "Arcanists study the hidden currents of the world and bend them with will and formula. "
                       "Fragile in a brawl, they command lightning, fog, binding runes and falling fire, "
                       "all drawn from a pool of Aether.",
        "hp": 7, "defense": 11, "attributes": {"strength": 8, "agility": 11, "wits": 15, "presence": 12},
        "resource": {"kind": "pool", "name": "Aether", "base": 4, "per_level": 2, "refresh": "recovery"},
        "abilities": [
            ability("Read the Weave", "Sense magic nearby.",
                    "You open your senses to the currents of magic around you.",
                    "Learn whether magic is present nearby and roughly what kind.", cost=0),
            ability("Spark Lance", "A crackling bolt of lightning.",
                    "A line of white light leaps from your fingertips to a foe you can see.",
                    "One target in sight takes 1d10 lightning damage on a hit.", cost=1),
            ability("Veil of Mist", "Conjure a concealing fog.",
                    "A thick, cold mist rolls out around a point you choose.",
                    "Fog fills about ten paces for a few minutes; nobody can see through it.", cost=2),
            ability("Binding Glyph", "A rune that holds a creature fast.",
                    "You trace a glowing rune that locks around a creature's limbs.",
                    "One creature is held in place until it breaks free with a strength roll.",
                    level=2, cost=3),
            ability("Starfall", "Call burning motes down from above.",
                    "Points of light gather overhead, then plunge in a fiery rain.",
                    "Everything in a small area takes 3d8 fire damage.", level=4, cost=5),
        ],
    },
    {
        "kind": "class", "name": "Mender",
        "summary": "A healer who mends wounds and wards off despair.",
        "description": "Menders may be temple healers, herbalists or field surgeons. They keep companions "
                       "alive, cleanse poison and fear, and in dire need can call a friend back from the brink.",
        "hp": 9, "defense": 12, "attributes": {"strength": 10, "agility": 10, "wits": 13, "presence": 14},
        "resource": {"kind": "uses", "refresh": "recovery"},
        "abilities": [
            ability("First Aid", "Patch up wounds after a fight.",
                    "With bandage and salve you tend a companion's hurts.",
                    "Out of combat, an ally regains 1d4 HP (once per ally after each fight)."),
            ability("Mend Wounds", "Knit flesh with a touch.",
                    "Warm light flows from your hands into an ally's wounds.",
                    "An ally you touch regains 2d8 HP.", uses=3),
            ability("Purge", "Cleanse poison or sickness.",
                    "You draw the taint out of an ally's body.",
                    "End the poisoned or sickened condition on an ally you touch.", uses=2),
            ability("Ward of Calm", "A circle of courage.",
                    "A steady hum surrounds your allies and quiets their fear.",
                    "For the rest of the fight, nearby allies can't be frightened and get +1 defense.",
                    level=2, uses=1),
            ability("Second Dawn", "Call back a friend who has just fallen.",
                    "You hold a fallen ally's hand and refuse to let them go.",
                    "An ally who died within the last minute returns to life with 1 HP.", level=5, uses=1),
        ],
    },
    {
        "kind": "class", "name": "Shade",
        "summary": "A quiet expert in stealth, locks and misdirection.",
        "description": "Shades are thieves, spies and scouts who get where they shouldn't and leave no trace. "
                       "Their tricks are few but potent, and they come back fresh at the start of each session.",
        "hp": 9, "defense": 13, "attributes": {"strength": 9, "agility": 15, "wits": 12, "presence": 11},
        "resource": {"kind": "uses", "refresh": "session"},
        "abilities": [
            ability("Quick Hands", "Locks, pockets and sleight of hand.",
                    "Your fingers are fast and sure with locks, purses and small objects.",
                    "+2 to rolls to pick locks, pick pockets or palm an object."),
            ability("Vanish", "Slip out of sight.",
                    "You melt into shadow or a crowd between one breath and the next.",
                    "You are hidden until you act or step into the open.", uses=2),
            ability("Exploit Opening", "Strike where a foe is distracted.",
                    "When an ally draws a foe's attention, you find the gap in its guard.",
                    "Against a foe an ally hit this round, deal an extra 1d8 damage.", uses=3),
            ability("Smoke and Mirrors", "A decoy of yourself.",
                    "A convincing double of you appears and draws every eye.",
                    "For a minute, a harmless illusion of you moves as you direct; foes may attack it instead.",
                    level=3, uses=1),
        ],
    },
    {
        "kind": "class", "name": "Wildcaller",
        "summary": "A friend of beasts and storms who calls on the wild.",
        "description": "Wildcallers live close to forest, marsh and sky. They talk with animals, bind foes "
                       "with thorns and summon wild allies, drawing on a pool of Kinship that refreshes "
                       "at the start of each session.",
        "hp": 10, "defense": 12, "attributes": {"strength": 11, "agility": 12, "wits": 12, "presence": 12},
        "resource": {"kind": "pool", "name": "Kinship", "base": 3, "per_level": 1, "refresh": "session"},
        "abilities": [
            ability("Beast Speech", "Talk with animals.",
                    "Animals understand you, and you them, though they only know what animals know.",
                    "Hold a simple conversation with any animal.", cost=0),
            ability("Thornbind", "Vines grip a foe.",
                    "Thorny vines burst from the ground and wrap around a creature.",
                    "One creature is held in place until it breaks free with a strength roll.", cost=1),
            ability("Call the Pack", "Summon a wild ally.",
                    "A wolf, hawk or bear answers your call and fights at your side.",
                    "A wild animal ally joins you for one fight or scene.", cost=2),
            ability("Stormcall", "Bring lightning down on your foes.",
                    "Dark clouds gather and strike at your command.",
                    "Up to three foes you can see each take 2d8 lightning damage.", level=3, cost=3),
        ],
    },
]

RACES: list[dict[str, Any]] = [
    {
        "kind": "race", "name": "Human",
        "summary": "Adaptable, stubborn and found everywhere.",
        "description": "Humans are short-lived and restless, and make up for it with grit and luck.",
        "hp": 1, "attributes": {},
        "abilities": [
            ability("Stubborn Luck", "Try again when it matters.",
                    "Sheer stubbornness turns a bad moment around.",
                    "Reroll one of your own rolls and keep the new result.", uses=1),
        ],
    },
    {
        "kind": "race", "name": "Elf",
        "summary": "Long-lived, keen-eyed folk of the old forests.",
        "description": "Elves remember centuries and notice what others miss: a footprint, a hidden door, "
                       "a lie in a smile.",
        "hp": 0, "attributes": {"agility": 1},
        "abilities": [
            ability("Keen Senses", "Notice what others miss.",
                    "Your eyes and ears are sharper than most.",
                    "+2 to rolls to spot hidden things, tracks or ambushes."),
            ability("Starlight Eyes", "See in dim light.",
                    "Moonlight or a distant torch is all you need.",
                    "You see in dim light as if it were daylight."),
        ],
    },
    {
        "kind": "race", "name": "Dwarf",
        "summary": "Sturdy mountain folk, hard to knock down.",
        "description": "Dwarves are stone-workers and keepers of grudges and oaths, famously hard to hurt.",
        "hp": 2, "attributes": {"strength": 1},
        "abilities": [
            ability("Stoneblood", "Shrug off a terrible blow.",
                    "Your body hardens like stone for an instant.",
                    "Halve the damage from one hit.", uses=1),
        ],
    },
    {
        "kind": "race", "name": "Kithri",
        "summary": "Small, quick, long-eared folk with a knack for escape.",
        "description": "Kithri are half the height of a human, quick-footed and curious, at home in "
                       "burrows, rafters and crowded markets.",
        "hp": 0, "attributes": {"agility": 1},
        "abilities": [
            ability("Slip Free", "Wriggle out of a grip or a tight spot.",
                    "You twist, duck and squeeze where others can't.",
                    "Escape a grab or squeeze through a gap a larger creature couldn't.", uses=2),
        ],
    },
    {
        "kind": "race", "name": "Emberborn",
        "summary": "People touched by an old fire spirit, warm to the touch.",
        "description": "Emberborn have ember-bright eyes and skin that glows faintly when they're angry. "
                       "Fire comes easily to them.",
        "hp": 1, "attributes": {"presence": 1},
        "abilities": [
            ability("Kindle", "A flame in your palm.",
                    "You call a small flame into your hand without burning yourself.",
                    "Light a flame that gives light or sets something small alight."),
            ability("Flare", "A burst of fire around you.",
                    "Heat erupts from your skin in a sudden wave.",
                    "Everyone beside you takes 2d6 fire damage.", uses=1),
        ],
    },
]


for _option in RACES + CLASSES:
    _option["key"] = key_of(_option["name"])


class CatalogError(ValueError):
    pass


def core() -> list[dict[str, Any]]:
    return RACES + CLASSES


# Dice and numbers in an effect: a theme may reword it but not change these.
NUMBERS = re.compile(r"\d+d\d+|\d+")


def apply_theme(option: dict[str, Any], theme: dict[str, Any] | None) -> dict[str, Any]:
    """A core option as a world names and describes it. Mechanics are kept; an ability whose
    reworded effect changes any dice or numbers keeps its core effect text."""
    if not theme:
        return option
    out = dict(option)
    for field, limit in (("name", 40), ("summary", 200), ("description", 600)):
        text = str(theme.get(field) or "").strip()
        if text:
            out[field] = text[:limit]
    if out["kind"] == "class" and out["resource"]["kind"] == "pool" and str(theme.get("pool_name") or "").strip():
        out["resource"] = out["resource"] | {"name": str(theme["pool_name"]).strip()[:24]}
    themed = {a.get("key"): a for a in theme.get("abilities") or [] if isinstance(a, dict)}
    abilities = []
    for a in option["abilities"]:
        t, a = themed.get(a["key"]) or {}, dict(a)
        for field, limit in (("name", 40), ("summary", 160), ("description", 400)):
            text = str(t.get(field) or "").strip()
            if text:
                a[field] = text[:limit]
        effect = str(t.get("effect") or "").strip()
        if effect and Counter(NUMBERS.findall(effect)) == Counter(NUMBERS.findall(a["effect"])):
            a["effect"] = effect[:300]
        abilities.append(a)
    out["abilities"] = abilities
    return out


def check_unique(options: list[dict[str, Any]]) -> None:
    """Players pick by name, so names must be unique per kind, and abilities per option."""
    seen = set()
    for o in options:
        if (o["kind"], o["name"].lower()) in seen:
            raise CatalogError(f"Two {o['kind']}s are called {o['name']}.")
        seen.add((o["kind"], o["name"].lower()))
        names = [a["name"].lower() for a in o["abilities"]]
        if len(names) != len(set(names)):
            raise CatalogError(f"Two of {o['name']}'s abilities share a name.")


def find(options: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any] | None:
    name = name.strip().lower()
    return next((o for o in options if o["kind"] == kind and o["name"].lower() == name), None)


def pool_max(cls: dict[str, Any], level: int) -> int:
    resource = cls.get("resource", {})
    if resource.get("kind") != "pool":
        return 0
    return resource["base"] + resource["per_level"] * (level - 1)


def abilities_for(option: dict[str, Any], level: int) -> list[dict[str, Any]]:
    """The option's abilities a character of `level` has, as rows to store on them."""
    if option["kind"] == "class":
        refresh = option["resource"]["refresh"]
        pool = option["resource"]["kind"] == "pool"
    else:
        refresh, pool = "recovery", False
    return [{
        "name": a["name"], "source": f"{option['kind']}: {option['name']}", "summary": a["summary"],
        "description": a["description"], "effect": a["effect"], "level": a["level"],
        "max_uses": None if pool else a["uses"], "cost": (a["cost"] or 0) if pool else None, "refresh": refresh,
    } for a in option["abilities"] if a["level"] <= level]


def _text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise CatalogError(f"{field} is required.")
    return text[:limit]


def _int(value: Any, field: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CatalogError(f"{field} must be a whole number.") from None
    return max(low, min(high, number))


def validate(option: dict[str, Any]) -> dict[str, Any]:
    """Checks and normalises an option (e.g. one a forged world wrote); clamps numbers to sane ranges."""
    kind = option.get("kind")
    if kind not in KINDS:
        raise CatalogError("kind must be race or class.")
    out: dict[str, Any] = {
        "kind": kind, "name": _text(option.get("name"), "name", 40),
        "key": key_of(str(option.get("name") or "")),
        "summary": _text(option.get("summary"), "summary", 200),
        "description": _text(option.get("description"), "description", 600),
        "attributes": {str(k)[:24]: _int(v, "attribute", -5, 20) for k, v in (option.get("attributes") or {}).items()},
    }
    if kind == "class":
        out["hp"] = _int(option.get("hp"), "hp", 4, 16)
        out["defense"] = _int(option.get("defense", 12), "defense", 8, 16)
        resource = option.get("resource") or {}
        refresh = resource.get("refresh", "recovery")
        if refresh not in REFRESHES:
            raise CatalogError("refresh must be recovery or session.")
        if resource.get("kind") == "pool":
            out["resource"] = {"kind": "pool", "name": _text(resource.get("name"), "pool name", 24),
                               "base": _int(resource.get("base"), "pool base", 1, 10),
                               "per_level": _int(resource.get("per_level"), "pool per level", 0, 3), "refresh": refresh}
        else:
            out["resource"] = {"kind": "uses", "refresh": refresh}
    else:
        out["hp"] = _int(option.get("hp", 0), "hp", 0, 3)
    pool = kind == "class" and out["resource"]["kind"] == "pool"
    abilities = option.get("abilities") or []
    if not 1 <= len(abilities) <= 6:
        raise CatalogError("An option needs between 1 and 6 abilities.")
    out["abilities"] = []
    for a in abilities:
        uses = a.get("uses")
        cost = a.get("cost")
        out["abilities"].append(ability(
            _text(a.get("name"), "ability name", 40), _text(a.get("summary"), "ability summary", 160),
            _text(a.get("description"), "ability description", 400), _text(a.get("effect"), "ability effect", 300),
            level=_int(a.get("level", 1), "ability level", 1, 10),
            uses=None if pool or uses in (None, 0) else _int(uses, "uses", 1, 5),
            cost=_int(cost or 0, "cost", 0, 6) if pool else None,
        ))
    if not any(a["level"] == 1 for a in out["abilities"]):
        raise CatalogError("At least one ability must be available at level 1.")
    return out
