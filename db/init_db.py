# db/init_db.py
import json
import asyncio
from tortoise import Tortoise
from db.models import Location, Event, World
from utils.llm import get_mistral

GEN_PROMPT = """
You are a worldbuilder for a high fantasy RPG, but also there's aliens.  Also, wolfman. Generate a setting that includes:

- The name and description of the world
- Three major regions or factions
- One unique magical element or threat
- One notable historical event

Respond ONLY in structured JSON like this (no extra narration):
{
  "world_name": "...",
  "description": "...",
  "regions": [{ "name": "...", "description": "..." }],
  "magic_or_threat": "...",
  "historical_event": "..."
}
"""


async def seed_world():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    llm = get_mistral()
    response = llm.invoke(GEN_PROMPT)
    print("🔍 Raw LLM output:\n", response)

    try:
        json_text = response.split("{", 1)[1]
        data = json.loads("{" + json_text)
    except json.JSONDecodeError as e:
        print("❌ Failed to parse JSON:\n", e)
        await Tortoise.close_connections()
        return

    await World.create(name=data["world_name"], description=data["description"])

    for region in data.get("regions", []):
        await Location.create(
            name=region["name"],
            region=data["world_name"],
            description=region["description"],
        )

    await Event.create(name="Historical Event", description=data["historical_event"])

    with open("lore/lore_seed.txt", "w") as f:
        f.write(f"World: {data['world_name']}\n")
        f.write(f"Description: {data['description']}\n")
        for r in data["regions"]:
            f.write(f"{r['name']}: {r['description']}\n")
        f.write(f"Magic/Threat: {data['magic_or_threat']}\n")
        f.write(f"Historical Event: {data['historical_event']}\n")

    print("✅ World generated and saved.")
    await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(seed_world())
