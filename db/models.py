# db/models.py
from tortoise import fields, models

class Character(models.Model):
    name = fields.CharField(max_length=100)
    race = fields.CharField(max_length=50)
    location = fields.CharField(max_length=100)
    description = fields.TextField()

class Location(models.Model):
    name = fields.CharField(max_length=100)
    region = fields.CharField(max_length=100)
    description = fields.TextField()

class Event(models.Model):
    title = fields.CharField(max_length=100)
    summary = fields.TextField()
    timestamp = fields.DatetimeField(auto_now_add=True)