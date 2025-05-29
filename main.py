# main.py
import asyncio
from tortoise import Tortoise
from db.models import Character, Location, Event
from utils.llm import get_mistral

async def main():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    llm = get_mistral()
    print("Welcome to your D&D world!")
    while True:
        user_input = input("You: ")
        if user_input.lower() in ["quit", "exit"]:
            break

        # In a real version, inject retrieved lore here
        prompt = f"You are a DM. Player says: '{user_input}'. Respond as a Dungeon Master."
        print("DM:", llm(prompt))

if __name__ == "__main__":
    asyncio.run(main())
