import asyncio
import json
import re
from tortoise import Tortoise
from db.models import Player, World, Event, Location, Character
from utils.llm import get_mistral
from utils.retrieval import get_retriever
from utils.prompt_template import PROMPT_TEMPLATE
from utils.select_character import select_character
from utils.region import get_or_create_location
from utils.player import select_or_create_player

import nest_asyncio

nest_asyncio.apply()


def extract_json(response: str):
    """Extract JSON-like content from the LLM response."""
    try:
        match = re.search(r"\{.*\}$", response.strip(), re.DOTALL)
        if match:
            return json.loads(match.group()), response[: match.start()].strip()
    except json.JSONDecodeError:
        pass
    return None, response.strip()


async def process_game_events(json_data, player):
    if "new_location" in json_data:
        loc = json_data["new_location"]
        await get_or_create_location(loc)

    if "new_event" in json_data:
        event = json_data["new_event"]
        await Event.create(name=event["title"], description=event["summary"])

    if "new_character" in json_data:
        char = json_data["new_character"]
        # Ensure location exists
        location, _ = await Location.get_or_create(name=char["location"])

        await Character.get_or_create(
            name=char["name"],
            defaults={
                "race": char.get("race", "unknown"),
                "description": char.get("description", "mysterious figure"),
                "location": location,
                "player": player,
            },
        )


async def main():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    print("Welcome to your D&D world!")

    # Get or create player
    player = await select_or_create_player()

    # Select or create character
    character, location = await select_character(player)
    print(f"\n🎮 Playing as {character.name} the {character.race} in {location.name}")

    # Main gameplay loop
    llm = get_mistral()
    retriever = await get_retriever()

    # Generate initial description
    world = await World.first()
    initial_prompt = PROMPT_TEMPLATE.format(
        user_input="Describe where I am and what I see.",
        context="",
        character_name=character.name,
        character_race=character.race,
        location=location.name,
        world_name=world.name,
        world_description=world.description,
    )
    initial_response = llm.invoke(initial_prompt)
    _, initial_text = extract_json(initial_response)
    print("DM:", initial_text)

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue

        if user_input.lower() in {"quit", "exit"}:
            print("👋 Exiting the game. Goodbye!")
            await Tortoise.close_connections()
            break

        relevant_docs = await retriever.ainvoke(user_input)
        context = "\n\n".join(doc.page_content for doc in relevant_docs)

        world = await World.first()
        prompt = PROMPT_TEMPLATE.format(
            user_input=user_input,
            context=context,
            character_name=character.name,
            character_race=character.race,
            location=location.name,
            world_name=world.name,
            world_description=world.description,
        )

        response = llm.invoke(prompt)
        json_data, text_response = extract_json(response)

        print("DM:", text_response)

        if json_data:
            await process_game_events(json_data, player)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Exiting the game. Goodbye!")
        asyncio.run(Tortoise.close_connections())
