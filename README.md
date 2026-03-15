# DevOps-Multi-Agent-System


## Commands

setting up the devops agent to run at any directory
`pip install -e .`

when updating the agent use both command
`pip uninstall devops-agent -y`
`pip install -e .`

testing the integration between devops_agent and the core
`pytest tests/cli_core_integration_test.py -v`