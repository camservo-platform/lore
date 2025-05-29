# Lore

**Lore** is a persistent, AI-powered storytelling engine for Dungeons & Dragons and other fantasy adventures. It combines a local vector database (Chroma), a local language model (via Ollama), and structured storage (Tortoise ORM) to create a continuously evolving world.

## Features

- 🌍 Persistent world-building powered by AI
- 🧙 Dynamic character creation and tracking
- 📚 World lore and event memory using vector retrieval
- ⚡ Uses local models (e.g., Mistral via Ollama)
- 🐢 Asynchronous ORM via Tortoise
- 🧠 Retrieval-Augmented Generation (RAG)

## Setup

### Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) with a model like `mistral`
- ChromaDB
- `make` (optional)

### Installation

```bash
git clone https://github.com/camservo-platform/lore
cd lore
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Seed the database

```bash
python -m db.init_db
```

This initializes the world and its regions, a magical threat, and a historical event using the LLM.

### Run the game

```bash
python main.py
```

Follow the prompts to create a new character and begin interacting with the world.

## Project Structure

```
lore/
├── db/
│   └── init_db.py
├── models/
│   └── models.py
├── utils/
│   ├── prompt_template.py
│   └── retrieval.py
├── main.py
├── requirements.txt
└── README.md
```

## Notes

- Uses `langchain_ollama.OllamaLLM` and `langchain_chroma.Chroma` for up-to-date vector retrieval and LLM calls.
- Metadata from user interactions is stored in SQLite via Tortoise ORM.
- Ensure your Ollama server is running before launching the app.

## License

MIT License
