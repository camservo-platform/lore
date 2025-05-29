from langchain_chroma import Chroma
from langchain_core.vectorstores import VectorStoreRetriever
from utils.llm import get_mistral_embeddings


async def get_retriever_and_db() -> tuple[VectorStoreRetriever, Chroma]:
    embedding_model = get_mistral_embeddings()
    db = Chroma(persist_directory=".chroma", embedding_function=embedding_model)
    return db.as_retriever(), db
