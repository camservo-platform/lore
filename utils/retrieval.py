from langchain_community.vectorstores import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from tortoise.models import Model
from datetime import datetime

from db.models import Location, Character, Event  # Ensure this matches your project
from langchain_core.vectorstores import VectorStoreRetriever


def doc_from_obj(obj, prefix: str) -> Document:
    metadata = {}
    for k, v in obj.__dict__.items():
        if k.startswith("_"):
            continue
        if isinstance(v, datetime):
            metadata[k] = v.isoformat()
        elif isinstance(v, (str, int, float, bool)) or v is None:
            metadata[k] = v
        else:
            metadata[k] = str(v)  # fallback, safe stringification
    return Document(
        page_content=f"{prefix}{obj.name}\n{obj.description}", metadata=metadata
    )


async def build_vectorstore() -> Chroma:
    embedding_model = OllamaEmbeddings(model="mistral")

    locations = await Location.all()
    characters = await Character.all()
    events = await Event.all()

    documents = (
        [doc_from_obj(loc, "Location: ") for loc in locations]
        + [doc_from_obj(char, "Character: ") for char in characters]
        + [doc_from_obj(evt, "Event: ") for evt in events]
    )

    db = Chroma.from_documents(documents, embedding_model, persist_directory=".chroma")
    return db


async def get_retriever() -> VectorStoreRetriever:
    db = await build_vectorstore()
    return db.as_retriever()
