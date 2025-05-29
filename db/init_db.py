import asyncio
from tortoise import Tortoise
from db.models import World, Region, Location, Event
from utils.llm import get_mistral
import json


async def seed_world():
    await Tortoise.init(db_url="sqlite://world.db", modules={"models": ["db.models"]})
    await Tortoise.generate_schemas()

    llm = get_mistral()

    prompt = """
You are a world-building assistant. Generate a Dungeons & Dragons world with the following structure:

{
  "world_name": "string",
  "description": "string",
  "regions": [
    {
      "name": "string",
      "description": "string"
    }
  ],
  "magic_or_threat": "string",
  "historical_event": "string"
}

Respond with only a JSON object that matches this format.
    """

    response = llm.invoke(prompt).strip()

    try:
        data = json.loads(response)
    except json.JSONDecodeError as e:
        print("❌ Failed to parse LLM response as JSON:")
        print(response)
        raise e

    # Save world
    world = await World.create(name=data["world_name"], description=data["description"])

    # Save regions and locations
    for region_data in data["regions"]:
        region = await Region.create(name=region_data["name"], description=region_data["description"], world=world)
        await Location.create(name=region_data["name"], description=region_data["description"], region=region)

    # Save major threat
    await Event.create(name="Major Threat", description=data["magic_or_threat"])

    # Save historical event
    await Event.create(name="Historical Event", description=data["historical_event"])

    print("✅ World seeding complete!")
    await Tortoise.close_connections()

if __name__ == "__main__":
    asyncio.run(seed_world())
