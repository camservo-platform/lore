# scripts/verify_characters.py

import asyncio
from tortoise import Tortoise
from utils.retrieval import build_vectorstore
from db.models import Character


async def verify_characters():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    print("🔍 Loading characters...")

    db_characters = await Character.all().prefetch_related("player", "location")
    vectorstore = await build_vectorstore()

    print(f"\n🔍 Found {len(db_characters)} characters.\n")

    # Get all documents from vectorstore (assumes persistent chroma with metadata)
    chroma_docs = vectorstore._collection.get(include=["metadatas", "documents"])
    llm_character_names = set()

    for doc, metadata in zip(chroma_docs["documents"], chroma_docs["metadatas"]):
        if doc.startswith("Character:"):
            name_line = doc.splitlines()[0].replace("Character: ", "").strip()
            llm_character_names.add(name_line.lower())

    for char in db_characters:
        is_npc = char.player is None
        llm_present = char.name.lower() in llm_character_names

        print(
            f"{char.name} ({char.race}) — "
            f"{'NPC' if is_npc else f'Player: {char.player.name}'} — "
            f"{'✅ In SQL DB' if char else '❌ Missing in DB'} — "
            f"{'✅ In LLM' if llm_present else '❌ Missing in LLM'}"
        )

    await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(verify_characters())
