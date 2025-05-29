# Lore

**Lore** is a persistent, AI-powered storytelling engine for Dungeons & Dragons and fantasy adventures. It combines a local vector database (Chroma), a local language model (via Ollama), and structured storage (Tortoise ORM) to maintain a continuously evolving game world.

## Features

- 🌍 Persistent world state with characters, events, and lore
- 🧙 Dynamic character creation per player
- 📚 Context-aware responses using vector retrieval (RAG)
- 🐢 Asynchronous ORM with Tortoise
- 🧠 Local LLM integration using Ollama (e.g., Mistral)
- ⚙️ Extendable prompt template system

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) (ensure a model like `mistral` is installed)
- SQLite (default)
- [Chroma](https://www.trychroma.com/) for vector store
- `make` (optional)

## Installation

```bash
git clone https://github.com/yourusername/lore.git
cd lore
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Seeding the World

Generate an initial world and populate the database with starter data:

```bash
python -m db.init_db
```

This will use the local LLM to define a world, regions, magical threats, and historical events.

## Running the Game

Launch the text-based RPG interface:

```bash
python main.py
```

Follow prompts to:

1. Enter your player name
2. Create a new character
3. Choose a starting location
4. Interact with the world via natural language

## Project Structure

```
lore/
├── db/
│   ├── init_db.py        # Seeds the world using the LLM
│   └── models.py         # Tortoise ORM models
├── utils/
│   ├── llm.py            # LLM wrapper
│   ├── prompt_template.py# Prompt string for generating DM responses
│   └── retrieval.py      # Vectorstore setup using Chroma
├── main.py               # Main game loop
├── requirements.txt
└── README.md
```

## Notes

- Ollama must be running (`ollama serve`) and a model like `mistral` must be installed.
- Vector search is powered by Chroma + LangChain with updated `langchain_ollama` and `langchain_chroma` support.
- This project uses an embedded SQLite database for simplicity. You can swap it out for PostgreSQL, etc.

## License

MIT License
