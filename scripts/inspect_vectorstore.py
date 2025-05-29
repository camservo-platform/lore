import os
from langchain_chroma import Chroma

# Update if your actual persist directory is elsewhere
PERSIST_DIR = ".chroma"

def main():
    if not os.path.exists(PERSIST_DIR):
        print(f"❌ No vector store found at: {PERSIST_DIR}")
        return

    print(f"🔍 Inspecting vector store at: {PERSIST_DIR}")
    store = Chroma(persist_directory=PERSIST_DIR)
    docs = store.get(include=["documents", "metadatas"])

    for i, (doc, meta) in enumerate(zip(docs["documents"], docs["metadatas"])):
        print(f"\n=== Document {i + 1} ===")
        print("📜 Content:\n", doc)
        print("🧾 Metadata:\n", meta)

if __name__ == "__main__":
    main()
