import asyncio
import json
import re
from tortoise import Tortoise
from db.models import World
from utils.llm import get_mistral
from utils.retriever import get_retriever_and_db
from utils.prompt_template import PROMPT_TEMPLATE
from utils.select_character import select_character
from utils.player import select_or_create_player
from utils.events import process_game_events
from utils.admin import handle_admin_command

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
    retriever, vector_db = await get_retriever_and_db()

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

        # if user_input.startswith("/"):
        #     admin_command = user_input[1:].strip()

        #     if admin_command.startswith("ask"):
        #         question = admin_command[len("ask"):].strip()
        #         prompt = f"You are the world engine. Answer this admin query directly:\n\n{question}"
        #         print("🛠️ Admin:", llm.invoke(prompt))
        #         continue

        #     elif admin_command == "help":
        #         print("Admin commands:\n  /ask <question>\n  /quit\n  /help")
        #         continue

        if user_input.startswith("/admin "):
            admin_input = user_input[len("/admin ") :]
            await handle_admin_command(admin_input, llm, vector_db)
            continue
            print("debug")

        if user_input.lower() in {"quit", "exit"}:
            print("👋 Exiting the game. Goodbye!")
            await Tortoise.close_connections()
            break

        print("debugging******")
        relevant_docs = await retriever.ainvoke(user_input)
        context = "\n\n".join(doc.page_content for doc in relevant_docs)

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
