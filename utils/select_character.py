from typing import Tuple
import questionary
from db.models import Player, Character, Location
from utils.character_creation import create_new_character


async def select_character(player: Player) -> Tuple[Character, Location]:
    characters = await Character.filter(player=player).prefetch_related("location")

    if characters:
        options = [
            f"{char.name} the {char.race} ({char.location.name})" for char in characters
        ]
        options.append("Create a new character")

        selection = questionary.select(
            "Choose a character or create a new one:", choices=options
        ).ask()

        if selection == "Create a new character":
            return await create_new_character(player)

        selected_index = options.index(selection)
        selected_char = characters[selected_index]
        location = selected_char.location
        return selected_char, location

    else:
        return await create_new_character(player)
