# vectorstore/chroma_setup.py
from langchain_community.document_loaders import TextLoader
from langchain_community.vectorstores import Chroma
from langchain_ollama import OllamaEmbeddings

loader = TextLoader("lore/lore_seed.txt")
docs = loader.load()
embeddings = OllamaEmbeddings(model="mistral")
Chroma.from_documents(docs, embeddings, persist_directory=".chroma")