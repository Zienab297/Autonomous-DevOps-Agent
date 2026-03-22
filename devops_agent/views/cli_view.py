import config


class CLIView:
    """
    All terminal I/O for the agent.
    Extend this to add: rich formatting, spinners, color, markdown rendering, etc.
    """

    DIVIDER = "─" * 50

    # ── Welcome / Exit ────────────────────────────────────────────────────────

    def show_welcome(self):
        print(f"\n{'═' * 50}")
        print(f"  {config.APP_NAME}")
        print(f"  Model : {config.MODEL}")
        print(f"  Tools : run_command, read_file, write_file,")
        print(f"          list_directory, git_status, git_diff, git_commit")
        print(f"  Type 'exit' to quit | 'clear' to reset | 'tools' to list")
        print(f"{'═' * 50}\n")

    def show_goodbye(self):
        print("\nAgent shutting down. Goodbye 👋\n")

    # ── Agent thinking / actions ──────────────────────────────────────────────

    def show_thinking(self):
        print(f"\n🧠  Thinking...", end="\r", flush=True)

    def clear_line(self):
        print(" " * 30, end="\r")

    def show_agent_text(self, text: str):
        print(f"\n🤖  {text}")

    def show_tool_call(self, tool_name: str, args: dict):
        args_str = ", ".join(f"{k}={repr(v)[:60]}" for k, v in args.items())
        print(f"\n⚙️   [{tool_name}] {args_str}")

    def show_tool_result(self, result: str):
        preview = result[:300] + "..." if len(result) > 300 else result
        print(f"    → {preview}")

    def show_iteration(self, n: int, max_n: int):
        print(f"\n{self.DIVIDER}")
        print(f"  Iteration {n}/{max_n}")
        print(self.DIVIDER)

    def show_task_complete(self):
        print(f"\n✅  Task complete.\n")

    def show_max_iterations(self):
        print(f"\n⚠️   Max iterations reached. Agent stopped.\n")

    # ── Info / Errors ─────────────────────────────────────────────────────────

    def show_error(self, message: str):
        print(f"\n❌  Error: {message}\n")

    def show_info(self, message: str):
        print(f"\nℹ️   {message}\n")

    def show_tools_list(self, tool_names: list):
        print(f"\n🛠️   Available tools:")
        for name in tool_names:
            print(f"     • {name}")
        print()

    # ── Input ─────────────────────────────────────────────────────────────────

    def get_input(self) -> str:
        try:
            return input("\nTask: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "exit"