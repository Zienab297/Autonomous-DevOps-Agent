import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from controllers.agent_controller import AgentController

def main():
    AgentController().run()

if __name__ == "__main__":
    main()