# utils/llm.py
# from langchain_community.llms import Ollama
from langchain_ollama import OllamaLLM

def get_mistral():
    return OllamaLLM(model="mistral")