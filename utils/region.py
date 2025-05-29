from db.models import Region, Location


async def get_or_create_location(loc_data: dict):
    region_name = loc_data.get("region", "Uncharted Lands")
    region, _ = await Region.get_or_create(
        name=region_name, defaults={"description": "A mysterious, unexplored region."}
    )

    location, _ = await Location.get_or_create(
        name=loc_data["name"],
        defaults={"description": loc_data["description"], "region": region},
    )
    return location
