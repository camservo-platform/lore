from db.models import Event, Location, Character
from utils.region import get_or_create_location


async def process_game_events(json_data, player):
    if "new_location" in json_data:
        loc = json_data["new_location"]
        await get_or_create_location(loc)

    if "new_event" in json_data:
        event = json_data["new_event"]
        await Event.create(name=event["title"], description=event["summary"])

    if "new_character" in json_data:
        char = json_data["new_character"]
        location = await Location.get_or_none(name=char["location"])
        await Character.get_or_create(
            name=char["name"],
            defaults={
                "race": char.get("race", "unknown"),
                "description": char.get("description", "mysterious figure"),
                "location": location,
                "player": None,  # 🚨 Ensures this is treated as an NPC
            },
        )
