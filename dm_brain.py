"""
Interfaces with the Google Gemini API to act as Dungeon Master.
"""
import os
import re
import discord
import google.generativeai as genai
from game_state import GameState

# Strips AUTOROLL tags from text before storing in conversation history
_AUTOROLL_STRIP_RE = re.compile(r'\[AUTOROLL:[^\]]*\]', re.IGNORECASE)

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

SYSTEM_PROMPT = """You are an expert, creative, and fair Dungeon Master for a Dungeons & Dragons 5th Edition campaign.

Your role:
- Narrate the world vividly but concisely (2-4 sentences for scene descriptions, 1-2 for NPC responses)
- Play all NPCs with distinct personalities
- Follow the rules of D&D 5e but prioritize fun over strict rules-lawyering
- When players attempt actions with uncertain outcomes, tell them what dice to roll and what DC to beat (e.g. "Roll Athletics, DC 14 to climb the wall")
- Keep track of the story and be consistent with established lore
- React dynamically to player choices — reward creativity
- Occasionally add atmosphere: sounds, smells, tension
- When combat starts, describe it cinematically
- End responses with a clear prompt or question so players know it's their turn

Constraint enforcement — this is important:
- Each message includes a PLAYER CAPABILITIES section listing exactly what each character can do
- If a player attempts to cast a spell they don't know, politely but firmly redirect them: explain the character wouldn't know that spell and ask what they actually do
- If a player tries to cast a spell but has no remaining spell slots at that level (or any level for a cantrip), narrate the failure and ask them to choose differently
- If a player tries to use a class feature that has no uses remaining, remind them it's exhausted and when it recharges
- If a player tries to take an action type (action/bonus action/reaction) that is already marked as used this turn, remind them and ask for a different choice
- If a player attempts something that requires an item they don't have in their inventory, narrate accordingly
- Be firm but in-character — don't break the fourth wall, frame corrections as the character realizing they can't do that

When players do something that requires a dice roll, format it like:
🎲 **[Player Name] needs to roll [Skill/Save/Attack] — DC [number]**

Then on the same line, append an AUTOROLL tag so the bot can roll automatically:
[AUTOROLL: player=<character_name>, dice=<N>d<X>, type=<adv|dis|normal>]

- Set type to adv if the character has advantage (e.g. from a spell, ability, or condition), dis for disadvantage, normal otherwise
- Use "all" as the player name for initiative rolls (rolls for every player)
- Valid dice: d4, d6, d8, d10, d12, d20, d100
- Only include the tag when you are actually requesting a dice roll, never for general narration

Examples:
  🎲 **Thorin needs to roll Athletics — DC 14** [AUTOROLL: player=Thorin, dice=1d20, type=normal]
  🎲 **Aria needs to roll Stealth — DC 12** [AUTOROLL: player=Aria, dice=1d20, type=adv]
  🎲 **Roll for Initiative!** [AUTOROLL: player=all, dice=1d20, type=normal]
  🎲 **Thorin rolls damage — Greataxe** [AUTOROLL: player=Thorin, dice=1d12, type=normal]

Keep your responses focused and engaging. You're running a session for friends, not writing a novel.
"""

def build_context_message(state: GameState) -> str:
    return f"""
=== CURRENT GAME STATE ===
Campaign: {state.campaign_name}
Session: {state.session_number}
Location: {state.current_location}

Players:
{state.player_summary()}

World Notes:
{state.world_notes if state.world_notes else "None yet."}

=== PLAYER CAPABILITIES (enforce these strictly) ===
{state.constraint_summary()}
====================================================
"""

async def get_dm_response(state: GameState, player_input: str, player_name: str) -> str:
    """Send a player's action/speech to Gemini and get a DM response."""

    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",  # Free tier friendly; swap to gemini-1.5-pro for richer responses
        system_instruction=SYSTEM_PROMPT,
    )

    # Build the full message with context injected
    context = build_context_message(state)
    full_input = f"{context}\n[{player_name} says/does]: {player_input}"

    # Add to history and send
    state.add_to_history("user", full_input)

    try:
        response = model.generate_content(
            contents=state.conversation_history,
            generation_config=genai.types.GenerationConfig(
                temperature=0.85,
                max_output_tokens=512,
            )
        )
        reply = response.text.strip()
    except Exception as e:
        reply = f"*(The DM pauses, distracted by an otherworldly force... Error: {e})*"

    # Strip the autoroll tag before storing in history so it doesn't pollute future context
    state.add_to_history("model", _AUTOROLL_STRIP_RE.sub('', reply).strip())
    state.save()
    return reply


async def get_level_up_info(char_class: str, new_level: int) -> str:
    """Ask Gemini what a character mechanically gains at this class level."""
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=(
            "You are a D&D 5e rules expert. List only mechanical changes — no flavor text. "
            "Use short bullet points. Be precise and accurate to the 2024 Player's Handbook rules."
        ),
    )
    prompt = (
        f"A {char_class} just reached level {new_level} in D&D 5th Edition. "
        f"List exactly what they gain: new class features, subclass feature notes (if applicable), "
        f"spell slot table changes (if a spellcasting class), proficiency bonus if it increases, "
        f"extra attack changes, hit die for HP roll, and anything else mechanical. "
        f"Keep each bullet to one line."
    )
    try:
        response = model.generate_content(
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                max_output_tokens=400,
            ),
        )
        return response.text.strip()
    except Exception as e:
        return f"*(Could not retrieve level-up details: {e})*"


async def announce_level_up(channel: discord.TextChannel, p: dict, old_level: int, new_level: int):
    """Post a level-up announcement embed to the given channel."""
    info = await get_level_up_info(p["char_class"], new_level)

    embed = discord.Embed(
        title="Level Up!",
        description=(
            f"**{p['character_name']}** has reached **Level {new_level}**!\n"
            f"*{p['race']} {p['char_class']}*"
        ),
        color=0xf1c40f,
    )
    embed.add_field(name=f"What's new at Level {new_level}", value=info, inline=False)
    embed.add_field(
        name="Next steps",
        value=(
            "Roll your hit die and use `!hp set <new_max>` to update HP.\n"
            "Use `!slot set <level> <max>` to update spell slots if your table changed.\n"
            "Use `!feature add` for any new limited-use features."
        ),
        inline=False,
    )
    if new_level - old_level > 1:
        embed.set_footer(text=f"Level {old_level} → {new_level}")

    await channel.send(embed=embed)


async def start_session(state: GameState) -> str:
    """Generate an opening narration for the session."""
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SYSTEM_PROMPT,
    )

    context = build_context_message(state)
    opener = (
        f"{context}\n"
        f"Begin Session {state.session_number} of {state.campaign_name}. "
        f"The players are gathered. Set the scene at {state.current_location} "
        f"and open the session dramatically. Welcome the adventurers."
    )

    state.add_to_history("user", opener)

    response = model.generate_content(
        contents=state.conversation_history,
        generation_config=genai.types.GenerationConfig(
            temperature=0.9,
            max_output_tokens=600,
        )
    )
    reply = response.text.strip()
    state.add_to_history("model", reply)
    state.save()
    return reply
