from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings
from langchain_core.vectorstores import VectorStoreRetriever


async def get_retriever_and_db() -> tuple[VectorStoreRetriever, Chroma]:
    embedding_model = OllamaEmbeddings(model="mistral")
    db = Chroma(persist_directory=".chroma", embedding_function=embedding_model)
    return db.as_retriever(), db
