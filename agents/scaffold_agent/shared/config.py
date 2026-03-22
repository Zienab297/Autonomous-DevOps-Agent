"""
shared/config.py
----------------
Central configuration for the Scaffold Agent.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    _dir = Path(__file__).resolve().parent
    while _dir != _dir.parent:
        if (_dir / ".env").exists():
            load_dotenv(_dir / ".env")
            break
        _dir = _dir.parent
except ImportError:
    pass


@dataclass
class Config:
    generation_model : str = "llama3.2:3b"
    max_file_size    : int = 50_000
    max_files        : int = 20

    scan_extensions  : list[str] = field(default_factory=lambda: [
        ".py", ".js", ".ts", ".java", ".go", ".rs",
        ".toml", ".yaml", ".yml", ".json", ".txt", ".md",
        "Dockerfile", "requirements.txt", "package.json",
        "pom.xml", "build.gradle", "go.mod", "Cargo.toml",
    ])

    # project size thresholds (number of files)
    small_project_max_files : int = 10
    medium_project_max_files: int = 30

    # cicd platform detection
    cicd_indicators : dict = field(default_factory=lambda: {
        "github": [".github/workflows"],
    })

    # database detection keywords
    db_indicators : dict = field(default_factory=lambda: {
        "postgres": ["psycopg2", "sqlalchemy", "pg", "postgres"],
        "mongo"   : ["pymongo", "mongoose", "mongodb"],
        "mysql"   : ["mysql", "pymysql", "mysqlclient"],
        "redis"   : ["redis", "aioredis"],
    })


def load_config() -> Config:
    return Config(
        generation_model         = os.getenv("GENERATION_MODEL", "llama3.2:3b"),
        small_project_max_files  = int(os.getenv("SMALL_PROJECT_MAX_FILES",  "10")),
        medium_project_max_files = int(os.getenv("MEDIUM_PROJECT_MAX_FILES", "30")),
    )