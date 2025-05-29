from tortoise import fields, models


class Player(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)

    characters: fields.ReverseRelation["Character"]  # <-- Add this line


class Character(models.Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=100)
    race = fields.CharField(max_length=50)
    description = fields.TextField()
    player = fields.ForeignKeyField("models.Player", related_name="characters")
    location = fields.ForeignKeyField("models.Location", related_name="characters")


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
