"""
Fetches and parses character data from the D&D Beyond character API.
Only works for publicly shared characters (no auth token required).
"""
import re
import aiohttp

# Matches both:
#   https://www.dndbeyond.com/characters/12345678
#   https://www.dndbeyond.com/profile/username/characters/12345678
CHAR_ID_RE = re.compile(r"dndbeyond\.com/(?:profile/[^/]+/)?characters/(\d+)")
API_URL = "https://character-service.dndbeyond.com/character/v5/character/{}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; DnD-Discord-Bot/1.0)",
    "Accept": "application/json",
}


def extract_character_id(url: str) -> str | None:
    match = CHAR_ID_RE.search(url)
    return match.group(1) if match else None


def _parse_spells(data: dict) -> tuple[list, dict]:
    """Extract known spells and spell slots from the character data.
    Returns (spells_known, spell_slots)."""
    spells_known = []
    seen = set()

    # classSpells groups spells by spellcasting class
    for class_entry in (data.get("classSpells") or []):
        for spell in (class_entry.get("spells") or []):
            defn = spell.get("definition") or {}
            name = defn.get("name")
            level = defn.get("level", 0)
            if name and name not in seen:
                seen.add(name)
                spells_known.append({"name": name, "level": level})

    # spellSlots: [{level: 1, used: 0, available: 4}, ...]
    spell_slots = {}
    for slot in (data.get("spellSlots") or []):
        lvl = str(slot.get("level", 0))
        available = slot.get("available", 0)
        used = slot.get("used", 0)
        if available > 0:
            spell_slots[lvl] = {"max": available, "remaining": max(0, available - used)}

    return spells_known, spell_slots


def _parse_features(data: dict) -> list:
    """Extract class/racial/feat actions with limited uses."""
    features = []
    seen = set()

    all_actions = []
    actions_obj = data.get("actions") or {}
    for group in actions_obj.values():
        if isinstance(group, list):
            all_actions.extend(group)

    for action in all_actions:
        name = action.get("name")
        if not name or name in seen:
            continue
        limited = action.get("limitedUse") or {}
        max_uses = limited.get("maxUses", 0)
        used = limited.get("usedUses", 0)
        if max_uses > 0:
            seen.add(name)
            features.append({
                "name": name,
                "max_uses": max_uses,
                "remaining": max(0, max_uses - used),
                "recharge": "long",  # default; DnD Beyond doesn't always expose recharge type
            })

    return features


def parse_character(data: dict) -> dict:
    """Extract the fields we care about from the raw D&D Beyond character JSON."""
    name = data.get("name", "Unknown Adventurer")

    # Race (fullName covers subrace, e.g. "Hill Dwarf"; fallback to baseName)
    race_obj = data.get("race") or {}
    race = race_obj.get("fullName") or race_obj.get("baseName", "Human")

    # Classes — supports multiclass (e.g. "Fighter/Wizard")
    classes = data.get("classes") or []
    if classes:
        char_class = "/".join(c["definition"]["name"] for c in classes)
        level = sum(c["level"] for c in classes)
    else:
        char_class = "Fighter"
        level = 1

    # HP — overrideHitPoints takes priority if the DM set a manual value
    max_hp = data.get("overrideHitPoints") or data.get("baseHitPoints") or 10
    removed_hp = data.get("removedHitPoints") or 0
    current_hp = max(0, max_hp - removed_hp)

    spells_known, spell_slots = _parse_spells(data)
    class_features = _parse_features(data)

    return {
        "character_name": name,
        "race": race,
        "char_class": char_class,
        "level": level,
        "max_hp": max_hp,
        "current_hp": current_hp,
        "dndbeyond_id": str(data.get("id", "")),
        "spells_known": spells_known,
        "spell_slots": spell_slots,
        "class_features": class_features,
    }


async def fetch_ddb_character(url: str) -> dict | None:
    """
    Fetch a D&D Beyond character by share URL.
    Returns parsed character dict on success, None on any failure.
    """
    char_id = extract_character_id(url)
    if not char_id:
        return None

    try:
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            async with session.get(
                API_URL.format(char_id),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                payload = await resp.json()
    except Exception:
        return None

    char_data = payload.get("data")
    if not char_data:
        return None

    return parse_character(char_data)
