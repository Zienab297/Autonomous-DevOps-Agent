from models.ai_model import AIModel
from models.conversation import ConversationModel
from views.cli_view import CLIView
import config


class ChatController:
    """
    Orchestrates the MVC loop.
    Add new commands, middleware, or logic here without touching Model or View.
    """

    def __init__(self):
        self.model = AIModel()
        self.conversation = ConversationModel(system_prompt=config.SYSTEM_PROMPT)
        self.view = CLIView()

    def run(self):
        self.view.show_welcome()

        while True:
            user_input = self.view.get_input()

            # ── Built-in commands ──────────────────────────────────────────
            if not user_input:
                continue

            if user_input.lower() in config.EXIT_COMMANDS:
                self.view.show_goodbye()
                break

            if user_input.lower() == "clear":
                self.conversation.clear()
                self.view.show_info("Conversation cleared.")
                continue

            if user_input.lower() == "history":
                self._show_history()
                continue

            # ── Normal chat flow ───────────────────────────────────────────
            self._handle_message(user_input)

    def _handle_message(self, user_input: str):
        """Add user message, call AI, display response."""
        self.conversation.add_user_message(user_input)
        self.view.show_thinking()

        try:
            response = self.model.get_response(self.conversation)
            self.conversation.add_assistant_message(response)
            self.view.clear_thinking()
            self.view.show_response(response)

        except RuntimeError as e:
            self.view.clear_thinking()
            self.view.show_error(str(e))
            # Remove the failed user message from history
            self.conversation.get_history().pop()

    def _show_history(self):
        """Display conversation history."""
        history = self.conversation.get_history()
        if not history:
            self.view.show_info("No conversation history yet.")
            return
        print(f"\n── History ({self.conversation.turn_count} turns) ──")
        for msg in history:
            prefix = "You" if msg.role == "user" else "AI"
            print(f"  {prefix}: {msg.content[:80]}{'...' if len(msg.content) > 80 else ''}")
        print()