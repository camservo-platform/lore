import asyncio
from tortoise import Tortoise
from db.models import Player, World, Character
from utils.llm import get_mistral
from utils.retrieval import get_retriever
from utils.prompt_template import PROMPT_TEMPLATE
from utils.character_creation import create_new_character
import questionary
import nest_asyncio

nest_asyncio.apply()


async def main():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    print("Welcome to your D&D world!")

    # Get or create player
    player_name = input("Enter your player name: ").strip()
    player, _ = await Player.get_or_create(name=player_name)

    # Show character options if they exist
    characters = await Character.filter(player=player).all()

    if characters:
        choices = [
            f"{char.name} the {char.race} ({(await char.location).name})"
            for char in characters
        ]
        choices.append("➕ Create a new character")

        selection = questionary.select(
            "Choose a character or create a new one:", choices=choices
        ).ask()

        if selection == "➕ Create a new character":
            character, location = await create_new_character(player)
        else:
            index = choices.index(selection)
            character = characters[index]
            location = await character.location
    else:
        character, location = await create_new_character(player)

    print(f"\n🎮 Playing as {character.name} the {character.race} in {location.name}")

    # Main gameplay loop
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
