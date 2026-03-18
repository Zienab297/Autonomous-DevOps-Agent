# DevOps-Multi-Agent-System


## SDK and CLI Setting up Commands 

setting up the devops agent to run at any directory
```bash
pip install -e .
```

when updating the agent use both command
```bash
pip uninstall devops-agent -y
pip install -e .
```

testing the integration between devops_agent and the core
```bash
pytest tests/cli_core_integration_test.py -v
```


## Running the Agents

**Knowledge Agent** — make sure Qdrant is running first
```bash
docker run -p 6333:6333 qdrant/qdrant
cd agents/knowledge_agent
python test_knowledge_agent.py
```

**Core** — make sure Qdrant is running first
```bash
docker run -p 6333:6333 qdrant/qdrant
cd core/test
python test_core.py
