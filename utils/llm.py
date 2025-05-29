# utils/llm.py
from langchain_ollama import OllamaLLM
from langchain_ollama import OllamaEmbeddings, OllamaLLM


def get_mistral():
    return OllamaLLM(model="mistral")


def get_mistral_embeddings():
    return OllamaEmbeddings(model="mistral")
