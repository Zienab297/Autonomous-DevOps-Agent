import os
from dotenv import load_dotenv

load_dotenv()  # Loads .env from project root

# --- AI Provider ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL = "llama-3.3-70b-versatile"
MAX_TOKENS = 1024

# --- App Settings ---
APP_NAME = "CLI Sim"
EXIT_COMMANDS = {"exit", "quit", "q", "bye"}

# --- System Prompt ---
# Customize this to change the AI's persona/role
SYSTEM_PROMPT = """You are a helpful assistant running in a CLI tool.
Be concise and direct. Format responses cleanly for terminal output."""