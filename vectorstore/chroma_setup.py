# vectorstore/chroma_setup.py
from langchain.vectorstores import Chroma
from langchain.embeddings import OllamaEmbeddings
from langchain.document_loaders import TextLoader

loader = TextLoader("lore/lore_seed.txt")
docs = loader.load()
embeddings = OllamaEmbeddings(model="mistral")
Chroma.from_documents(docs, embeddings, persist_directory=".chroma")