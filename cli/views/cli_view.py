import config


class CLIView:
    """
    Handles all terminal I/O.
    Extend this to add: rich formatting, colors, spinners, markdown rendering, etc.
    """

    # ── Output ────────────────────────────────────────────────────────────────

    def show_welcome(self):
        print(f"\n{'=' * 50}")
        print(f"  {config.APP_NAME}")
        print(f"  Model: {config.MODEL}")
        print(f"  Type 'exit' to quit | 'clear' to reset chat")
        print(f"{'=' * 50}\n")

    def show_response(self, text: str):
        print(f"\n🤖  {text}\n")

    def show_error(self, message: str):
        print(f"\n❌  Error: {message}\n")

    def show_info(self, message: str):
        print(f"\nℹ️   {message}\n")

    def show_goodbye(self):
        print("\nGoodbye 👋\n")

    def show_thinking(self):
        print("⏳  Thinking...", end="\r", flush=True)

    def clear_thinking(self):
        print(" " * 20, end="\r")  # Overwrite the thinking line

    # ── Input ─────────────────────────────────────────────────────────────────

    def get_input(self) -> str:
        try:
            return input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "exit"