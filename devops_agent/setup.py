from setuptools import setup, find_packages

setup(
    name="devops-agent",
    version="0.1.0",
    py_modules=["devops", "devops_agent_sdk"],  # expose both
    packages=find_packages(),
    install_requires=[
        "groq>=0.4.0",
        "python-dotenv>=1.0.0",
    ],
    entry_points={
        "console_scripts": [
            "devops=devops:main",
        ],
    },
)