# main.py
import asyncio
import json
from tortoise import Tortoise
from db.models import Character, Location, Event
from utils.llm import get_mistral
from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

async def main():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    llm = get_mistral()
    retriever = Chroma(
        persist_directory=".chroma",
        embedding_function=OllamaEmbeddings(model="mistral")
    ).as_retriever()

    print("Welcome to your D&D world!")

    while True:
        user_input = input("You: ")
        if user_input.lower() in ["quit", "exit"]:
            break

        relevant = retriever.get_relevant_documents(user_input)
        context = "\n".join(doc.page_content for doc in relevant)

        first_location = await Location.first()
        world_name = first_location.region if first_location else "the world"

        prompt = f"""
You are a Dungeon Master in the world of {world_name}.
Player: "{user_input}"

Relevant Lore:
{context}

Respond with a narrative followed by a JSON block like:
{{
  "new_event": {{...}},
  "new_location": {{...}},
  "new_character": {{...}}
}}
"""

        raw_output = llm.invoke(prompt)
        print("DM:", raw_output)

        try:
            json_text = raw_output.split("{", 1)[1]
            json_data = json.loads("{" + json_text)

            if "new_event" in json_data:
                await Event.create(**json_data["new_event"])
            if "new_location" in json_data:
                await Location.create(**json_data["new_location"])
            if "new_character" in json_data:
                await Character.create(**json_data["new_character"])

        except Exception as e:
            print("⚠️ Could not parse LLM world update:", e)

if __name__ == "__main__":
    asyncio.run(main())
