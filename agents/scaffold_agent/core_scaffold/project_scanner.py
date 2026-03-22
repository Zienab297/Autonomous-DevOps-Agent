from setup_path import *
"""
core_scaffold/project_scanner.py
---------------------------------
Scans a project directory and builds a ProjectContext.

Steps:
  1. Walk the project directory
  2. Read relevant files
  3. Detect language, framework, entry point, port
  4. Auto-detect deploy config (size, target, cicd, database)
  5. Return ProjectContext ready for the LLM
"""

import os
from pathlib import Path

from shared.models import (
    ProjectContext, Language, Framework,
    DeployConfig, ProjectSize, DeployTarget, CicdPlatform,
)
from shared.config import Config


# ── language detection ────────────────────────────────────────────────────────

LANGUAGE_SIGNALS = {
    Language.PYTHON : ["requirements.txt", "setup.py", "pyproject.toml", "*.py"],
    Language.NODE   : ["package.json", "*.js", "*.ts"],
    Language.JAVA   : ["pom.xml", "build.gradle", "*.java"],
    Language.GO     : ["go.mod", "*.go"],
    Language.RUST   : ["Cargo.toml", "*.rs"],
}

# ── framework detection ───────────────────────────────────────────────────────

FRAMEWORK_SIGNALS = {
    Framework.FASTAPI  : ["fastapi", "uvicorn"],
    Framework.DJANGO   : ["django", "manage.py"],
    Framework.FLASK    : ["flask"],
    Framework.EXPRESS  : ["express"],
    Framework.NEXTJS   : ["next", "next.config"],
    Framework.NUXT     : ["nuxt", "nuxt.config"],
    Framework.REACT    : ["react", "react-dom"],
    Framework.VUE      : ["vue", "@vue"],
    Framework.ANGULAR  : ["@angular/core"],
    Framework.SVELTE   : ["svelte"],
    Framework.SPRING   : ["spring-boot", "springframework"],
    Framework.GIN      : ["github.com/gin-gonic/gin"],
}

IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "env", "dist", "build", ".idea", ".vscode", "*.egg-info",
}


class ProjectScanner:

    def __init__(self, config: Config):
        self.config = config

    def scan(self, project_path: str) -> ProjectContext:
        path = Path(project_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Project path not found: {project_path}")

        print(f"[ProjectScanner] Scanning: {path}")

        # step 1: collect files
        files_snapshot = self._collect_files(path)
        print(f"[ProjectScanner] Collected {len(files_snapshot)} files")

        # step 2: detect language
        language = self._detect_language(files_snapshot)
        print(f"[ProjectScanner] Language: {language.value}")

        # step 3: detect framework
        framework = self._detect_framework(files_snapshot)
        print(f"[ProjectScanner] Framework: {framework.value}")

        # step 4: detect entry point
        entry_point = self._detect_entry_point(files_snapshot, language)
        print(f"[ProjectScanner] Entry point: {entry_point}")

        # step 5: detect port
        port = self._detect_port(files_snapshot)
        print(f"[ProjectScanner] Port: {port}")

        # step 6: collect dependencies
        dependencies = self._collect_dependencies(files_snapshot)

        # step 7: auto-detect deploy config
        deploy_config = self._detect_deploy_config(files_snapshot, dependencies, path)
        print(f"[ProjectScanner] Project size  : {deploy_config.project_size.value}")
        print(f"[ProjectScanner] Deploy target : {deploy_config.deploy_target.value}")
        print(f"[ProjectScanner] CI/CD         : {deploy_config.cicd_platform.value}")
        print(f"[ProjectScanner] Database      : {deploy_config.database_type}")

        return ProjectContext(
            project_path   = str(path),
            language       = language,
            framework      = framework,
            entry_point    = entry_point,
            port           = port,
            dependencies   = dependencies,
            files_snapshot = files_snapshot,
            deploy_config  = deploy_config,
        )

    # ── file collection ───────────────────────────────────────────────────────

    def _collect_files(self, path: Path) -> dict[str, str]:
        files = {}
        count = 0

        for root, dirs, filenames in os.walk(path):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

            for filename in filenames:
                if count >= self.config.max_files:
                    break

                filepath  = Path(root) / filename
                relative  = str(filepath.relative_to(path))

                if not self._should_include(filename):
                    continue

                try:
                    if filepath.stat().st_size > self.config.max_file_size:
                        continue
                    content = filepath.read_text(encoding="utf-8", errors="ignore")
                    files[relative] = content
                    count += 1
                except Exception:
                    continue

        return files

    def _should_include(self, filename: str) -> bool:
        for ext in self.config.scan_extensions:
            if ext.startswith("*"):
                if filename.endswith(ext[1:]):
                    return True
            else:
                if filename == ext or filename.endswith(ext):
                    return True
        return False

    # ── language detection ────────────────────────────────────────────────────

    def _detect_language(self, files: dict) -> Language:
        scores = {lang: 0 for lang in Language}

        for filename in files:
            fname = Path(filename).name
            for lang, signals in LANGUAGE_SIGNALS.items():
                for signal in signals:
                    if signal.startswith("*"):
                        if fname.endswith(signal[1:]):
                            scores[lang] += 1
                    else:
                        if fname == signal:
                            scores[lang] += 3

        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else Language.UNKNOWN

    # ── framework detection ───────────────────────────────────────────────────

    def _detect_framework(self, files: dict) -> Framework:
        all_content = " ".join(files.values()).lower()

        for framework, signals in FRAMEWORK_SIGNALS.items():
            if any(signal.lower() in all_content for signal in signals):
                return framework

        return Framework.UNKNOWN

    # ── entry point detection ─────────────────────────────────────────────────

    def _detect_entry_point(self, files: dict, language: Language) -> str:
        candidates = {
            Language.PYTHON : ["main.py", "app.py", "run.py", "server.py", "manage.py"],
            Language.NODE   : ["index.js", "app.js", "server.js", "index.ts"],
            Language.JAVA   : ["Application.java", "Main.java"],
            Language.GO     : ["main.go"],
            Language.RUST   : ["main.rs"],
        }

        for candidate in candidates.get(language, []):
            for filename in files:
                if Path(filename).name == candidate:
                    return filename

        return ""

    # ── port detection ────────────────────────────────────────────────────────

    def _detect_port(self, files: dict) -> int:
        import re
        all_content = " ".join(files.values())

        patterns = [
            r'port[=:\s]+(\d{4,5})',
            r'PORT[=:\s]+(\d{4,5})',
            r'listen[(\s]+(\d{4,5})',
            r':(\d{4,5})',
        ]

        for pattern in patterns:
            match = re.search(pattern, all_content)
            if match:
                port = int(match.group(1))
                if 1024 <= port <= 65535:
                    return port

        defaults = {
            Language.PYTHON : 8000,
            Language.NODE   : 3000,
            Language.JAVA   : 8080,
            Language.GO     : 8080,
        }
        return defaults.get(Language.UNKNOWN, 8000)

    # ── dependencies collection ───────────────────────────────────────────────

    def _collect_dependencies(self, files: dict) -> list[str]:
        deps = []

        if "requirements.txt" in files:
            for line in files["requirements.txt"].splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    deps.append(line.split("==")[0].split(">=")[0].strip())

        if "package.json" in files:
            import json
            try:
                pkg = json.loads(files["package.json"])
                deps.extend(list(pkg.get("dependencies", {}).keys()))
            except Exception:
                pass

        return deps[:30]

    # ── deploy config auto-detection ──────────────────────────────────────────

    def _detect_deploy_config(
        self,
        files       : dict,
        dependencies: list[str],
        project_path: Path,
    ) -> DeployConfig:

        # -- project size (based on file count) --
        file_count = len(files)
        if file_count <= self.config.small_project_max_files:
            project_size = ProjectSize.SMALL
        elif file_count <= self.config.medium_project_max_files:
            project_size = ProjectSize.MEDIUM
        else:
            project_size = ProjectSize.PRODUCTION

        # -- deploy target --
        # if k8s folder already exists → kubernetes
        # otherwise default to docker
        k8s_exists = any("k8s" in f or "kubernetes" in f for f in files)
        deploy_target = DeployTarget.KUBERNETES if k8s_exists else DeployTarget.DOCKER

        # -- cicd platform --
        # check if .github/workflows already exists in project
        github_exists = any(".github" in f for f in files)
        cicd_platform = CicdPlatform.GITHUB if github_exists else CicdPlatform.GITHUB
        # always default to github since that's the only platform we support

        # -- database detection --
        all_deps    = " ".join(dependencies).lower()
        all_content = " ".join(files.values()).lower()
        combined    = all_deps + " " + all_content

        has_database  = False
        database_type = "none"

        for db, keywords in self.config.db_indicators.items():
            if any(kw in combined for kw in keywords):
                has_database  = True
                database_type = db
                break

        return DeployConfig(
            project_size  = project_size,
            deploy_target = deploy_target,
            cicd_platform = cicd_platform,
            has_database  = has_database,
            database_type = database_type,
        )