from utils.llm import get_mistral
from utils.retriever import get_retriever_and_db
from langchain_core.documents import Document
from db.models import Character, Location, Event

# from utils.prompt_template import PROMPT_TEMPLATE
from prompts.admin_prompt import ADMIN_PROMPT_TEMPLATE
from langchain_core.vectorstores import VectorStoreRetriever
import re, json

llm = get_mistral()
retriever, vector_db = None, None


async def setup():
    global retriever, vector_db
    retriever, vector_db = await get_retriever_and_db()


def extract_json(response: str):
    """Extract JSON-like content from the LLM response."""
    try:
        match = re.search(r"\{.*\}$", response.strip(), re.DOTALL)
        if match:
            return json.loads(match.group()), response[: match.start()].strip()
    except json.JSONDecodeError:
        pass
    return None, response.strip()


async def handle_admin_command(command: str, llm, vector_db):
    if command.startswith("ask "):
        query = command[len("ask ") :].strip()
        print("🤖 Admin Ask:")

        # Use vector DB to retrieve relevant documents from the game world
        retriever: VectorStoreRetriever = vector_db.as_retriever()
        relevant_docs = await retriever.ainvoke(query)
        context = "\n\n".join(doc.page_content for doc in relevant_docs)

        # Use a neutral character/world prompt for admin queries
        prompt = ADMIN_PROMPT_TEMPLATE.format(
            user_input=query,
            context=context
        )

        response = llm.invoke(prompt)
        _, text_response = extract_json(response)
        print(text_response)

    elif command.startswith("list characters"):
        print("📋 Characters in vectorstore:")
        results = vector_db.similarity_search("list all characters", k=10)
        for doc in results:
            print("-", doc.page_content.split("\n")[0])

    elif command.startswith("search "):
        term = command[len("search ") :].strip()
        results = vector_db.similarity_search(term, k=5)
        print(f"🔍 Search results for '{term}':")
        for doc in results:
            print("-", doc.page_content.split("\n")[0])

    else:
        print(f"⚠️ Unknown admin command: {command}")


async def ask_world_question(question: str) -> str:
    prompt = f"""
    You are the omniscient world engine. A human is asking you a direct question about the state, history, or structure of the world. 
    Answer plainly and helpfully without assuming a character role.

    Question: {question}
    """
    return llm.invoke(prompt)


async def search_vectorstore(query: str) -> str:
    if retriever is None:
        await setup()

    results = await retriever.ainvoke(query)
    if not results:
        return "No documents found."

    return "\n---\n".join(doc.page_content for doc in results)


async def list_characters() -> str:
    characters = await Character.all().prefetch_related("location", "player")
    lines = []
    for char in characters:
        role = "NPC" if char.player is None else f"Player: {char.player.name}"
        lines.append(f"- {char.name} ({char.race}) in {char.location.name} — {role}")
    return "\n".join(lines) if lines else "No characters found."
