# utils/llm.py
from langchain_ollama import OllamaLLM

def get_mistral():
    return OllamaLLM(model="mistral")