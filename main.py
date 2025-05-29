import asyncio
from tortoise import Tortoise
from db.models import Player, Character, Location, Event, World
from utils.llm import get_mistral
from utils.retrieval import get_retriever
from utils.prompt_template import PROMPT_TEMPLATE


async def main():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    print("Welcome to your D&D world!")

    # Get or create player
    player_name = input("Enter your player name: ").strip()
    player, _ = await Player.get_or_create(name=player_name)

    print("\nCreating a new character...")
    name = input("Character name: ").strip()

    races = ["human", "elf", "orc", "dwarf", "halfling"]
    print("\nChoose a race:")
    for r in races:
        print(f"- {r}")
    while True:
        race = input("Character race: ").strip().lower()
        if race in races:
            break
        print("❌ Invalid race. Please choose from the list above.")

    locations = await Location.all()
    print("\nAvailable starting locations:")
    for loc in locations:
        print(f"- {loc.name}")
    location = None
    while not location:
        choice = input("Starting location: ").strip()
        location = next((loc for loc in locations if loc.name == choice), None)
        if not location:
            print("❌ Invalid location. Try again.")

    character = await Character.create(
        name=name, race=race, location=location, description="An eager adventurer."
    )
    print(f"\n🎮 Playing as {name} the {race} in {location.name}")

    # Main gameplay loop
    llm = get_mistral()
    llm = get_mistral()
    retriever = await get_retriever()

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue

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

        print("DM:", response.split("```")[0].strip())


if __name__ == "__main__":
    asyncio.run(main())
