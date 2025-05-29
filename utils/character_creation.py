# utils/character_creation.py

import questionary
from db.models import Character, Location
from utils.retrieval import add_doc


async def create_new_character(player):
    name = questionary.text("Character name:").ask()

    races = ["human", "elf", "orc", "dwarf", "halfling"]
    race = questionary.select("Choose a race:", choices=races).ask()

    locations = await Location.all()
    location_names = [loc.name for loc in locations]
    location_choice = questionary.select(
        "Choose a starting location:", choices=location_names
    ).ask()

    location = next(loc for loc in locations if loc.name == location_choice)

    character = await Character.create(
        name=name,
        race=race,
        location=location,
        description="An eager adventurer.",
        player=player,
    )

    await add_doc(character, "Character: ")  # ✅ embed new character in vector DB

    return character, location