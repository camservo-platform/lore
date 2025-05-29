from db.models import Player
import questionary


async def select_or_create_player():
    players = await Player.all()
    choices = [f"{player.name}" for player in players]
    choices.append("Create a new player")

    selected = questionary.select(
        "Choose a player or create a new one:", choices=choices
    ).ask()

    if selected == "Create a new player":
        name = questionary.text("Enter your player name:").ask()
        player, _ = await Player.get_or_create(name=name)
    else:
        player = await Player.get(name=selected)

    return player
