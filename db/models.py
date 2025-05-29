from tortoise import fields, models


class Player(models.Model):
    name = fields.CharField(max_length=100, unique=True)
    character_ids = fields.JSONField(default=list)  # list of character IDs
    current_character = fields.CharField(
        max_length=100, null=True
    )  # store character name or ID


class Character(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    race = fields.CharField(max_length=50)
    location = fields.CharField(max_length=100)
    description = fields.TextField()
    inventory = fields.JSONField(default=list)


class Location(models.Model):
    name = fields.CharField(max_length=100)
    region = fields.CharField(max_length=100)
    description = fields.TextField()


class Event(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(
        max_length=100
    )  # Compatible with retrieval and existing code
    description = fields.TextField()
    timestamp = fields.DatetimeField(auto_now_add=True)


class World(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=255)
    description = fields.TextField()
