from setup_path import *
"""
core_scaffold/scaffold_agent.py
--------------------------------
Main orchestrator for the Scaffold Agent.
"""

import ollama
from shared.models import ProjectContext, ScaffoldResult, DeployConfig, DeployTarget
from shared.config import Config
from core_scaffold.project_scanner import ProjectScanner
from core_scaffold.file_generator  import parse_llm_response, write_files


class ScaffoldAgent:

    def __init__(self, config: Config):
        self.config  = config
        self.scanner = ProjectScanner(config)

    def run(self, project_path: str, dry_run: bool = False) -> ScaffoldResult:
        print(f"\n[ScaffoldAgent] Starting for: {project_path}")

        # step 1: scan project + auto-detect deploy config
        context = self.scanner.scan(project_path)

        # step 2: build prompt
        prompt = self._build_prompt(context)

        # step 3: call LLM
        print(f"[ScaffoldAgent] Calling Ollama {self.config.generation_model}...")
        response = ollama.chat(
            model=self.config.generation_model,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = response["message"]["content"]

        # DEBUG: show what LLM returned
        print("\n[DEBUG] LLM Response (first 800 chars):")
        print("-" * 50)
        print(response_text[:800])
        print("-" * 50)

        # step 4: parse generated files
        generated_files = parse_llm_response(response_text)
        print(f"[ScaffoldAgent] Parsed {len(generated_files)} files from LLM response")

        # step 4b: fix common LLM mistakes in Dockerfile
        generated_files = self._fix_generated_files(generated_files, context.project_name)

        # step 5: build result
        result = ScaffoldResult(
            project_path    = project_path,
            language        = context.language,
            framework       = context.framework,
            generated_files = generated_files,
        )

        # step 6: write files
        write_files(result, dry_run=dry_run)

        print(f"\n[ScaffoldAgent] Done — {result.summary()}")

        # step 7: print post-generation checklist for user
        if not dry_run:
            self._print_post_generation_message(context, context.project_name)

        return result

    # ── prompt builder ────────────────────────────────────────────────────────

    def _build_prompt(self, context: ProjectContext) -> str:
        deploy       = context.deploy_config
        project_name = context.project_name
        db_block     = self._db_compose_block(deploy, project_name)
        entry        = context.entry_point.replace(".py", "")

        return f"""You are a DevOps engineer. Your only job is to output deployment files.

IMPORTANT:
- Do NOT write Python code or scripts
- Do NOT write explanations or introductions
- Do NOT use markdown headers like ### or numbered lists like 1.
- Output ONLY file contents using the FILE: format below
- Start your response immediately with: FILE: Dockerfile
- In the Dockerfile, ALWAYS write "COPY . ." — never "COPY {project_name} ." or any other folder name

Project name : {project_name}
Language     : {context.language.value}
Framework    : {context.framework.value}
Entry point  : {context.entry_point}
Port         : {context.port}

Output all 7 files using this EXACT format for each file:

FILE: Dockerfile
```
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {context.port}
CMD ["uvicorn", "{entry}:app", "--host", "0.0.0.0", "--port", "{context.port}"]
```
DESCRIPTION: Docker image for {project_name}

FILE: .dockerignore
```
__pycache__
*.pyc
*.pyo
.git
.env
.env.*
.pytest_cache
dist
build
*.log
```
DESCRIPTION: Files to exclude from Docker image

FILE: docker-compose.yml
```
version: "3.9"
services:
  {project_name}:
    build: .
    container_name: {project_name}
    ports:
      - "{context.port}:{context.port}"
    restart: unless-stopped
    env_file:
      - .env
{db_block}
```
DESCRIPTION: Docker Compose for {project_name}

FILE: .github/workflows/deploy.yml
```
name: Deploy to Production
on:
  push:
    branches:
      - main
  workflow_dispatch:
    inputs:
      environment:
        description: Target environment
        required: false
        default: production
      triggered_by:
        description: Who triggered this
        required: false
        default: devops-agent-cli
jobs:
  build-and-push:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          username: ${{{{ secrets.DOCKER_USERNAME }}}}
          password: ${{{{ secrets.DOCKER_PASSWORD }}}}
      - run: docker build -t ${{{{ secrets.DOCKER_USERNAME }}}}/{project_name}:latest .
      - run: docker push ${{{{ secrets.DOCKER_USERNAME }}}}/{project_name}:latest
```
DESCRIPTION: GitHub Actions CI/CD pipeline

FILE: k8s/deployment.yaml
```
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {project_name}
  labels:
    app: {project_name}
spec:
  replicas: 2
  selector:
    matchLabels:
      app: {project_name}
  template:
    metadata:
      labels:
        app: {project_name}
    spec:
      containers:
        - name: {project_name}
          image: ${{{{ secrets.DOCKER_USERNAME }}}}/{project_name}:latest
          ports:
            - containerPort: {context.port}
          livenessProbe:
            httpGet:
              path: /health
              port: {context.port}
            initialDelaySeconds: 10
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: {context.port}
            initialDelaySeconds: 5
            periodSeconds: 10
```
DESCRIPTION: Kubernetes Deployment for {project_name}

FILE: k8s/service.yaml
```
apiVersion: v1
kind: Service
metadata:
  name: {project_name}
spec:
  selector:
    app: {project_name}
  ports:
    - protocol: TCP
      port: {context.port}
      targetPort: {context.port}
  type: ClusterIP
```
DESCRIPTION: Kubernetes Service for {project_name}

FILE: k8s/ingress.yaml
```
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {project_name}
  annotations:
    nginx.ingress.kubernetes.io/rewrite-target: /
spec:
  rules:
    - host: example.com
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {project_name}
                port:
                  number: {context.port}
```
DESCRIPTION: Kubernetes Ingress for {project_name}

Now output the 7 files above with correct values.
Use project name "{project_name}" and port "{context.port}" everywhere.
START with FILE: Dockerfile
"""

    # ── generated file fixes ──────────────────────────────────────────────────

    def _fix_generated_files(self, files, project_name: str):
        """
        Fix common LLM mistakes in generated files.
        Runs after parse, before write — acts as a safety net.
        """
        import re
        for gf in files:
            if gf.filename == "Dockerfile":
                original = gf.content
                # Fix: LLM sometimes writes "COPY <project_name> ." instead of "COPY . ."
                gf.content = re.sub(
                    rf'COPY\s+{re.escape(project_name)}\s+\.',
                    'COPY . .',
                    gf.content,
                )
                # Fix: also catch any other "COPY <single-word-no-dot> ." that isn't requirements.txt
                gf.content = re.sub(
                    r'COPY\s+(?!requirements\.txt)(?!\.\s)([a-zA-Z0-9_-]+)\s+\.',
                    'COPY . .',
                    gf.content,
                )
                if gf.content != original:
                    print(f"[ScaffoldAgent] Fixed Dockerfile COPY command (LLM used project name instead of '.')")
        return files

    # ── helpers ───────────────────────────────────────────────────────────────

    def _db_compose_block(self, deploy: DeployConfig, project_name: str) -> str:
        """Add database service to docker-compose if detected."""
        if not deploy.has_database:
            return ""

        blocks = {
            "postgres": f"""
  db:
    image: postgres:15-alpine
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: {project_name}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

volumes:
  postgres_data:""",

            "mongo": """
  db:
    image: mongo:6
    volumes:
      - mongo_data:/data/db
    restart: unless-stopped

volumes:
  mongo_data:""",

            "mysql": f"""
  db:
    image: mysql:8
    environment:
      MYSQL_ROOT_PASSWORD: password
      MYSQL_DATABASE: {project_name}
    volumes:
      - mysql_data:/var/lib/mysql
    restart: unless-stopped

volumes:
  mysql_data:""",

            "redis": """
  redis:
    image: redis:7-alpine
    restart: unless-stopped""",
        }

        return blocks.get(deploy.database_type, "")

    # ── post generation message ───────────────────────────────────────────────

    def _print_post_generation_message(self, context, project_name: str) -> None:
        """Print a checklist message for the user after files are generated."""

        print("\n")
        print("=" * 60)
        print("  ACTION REQUIRED — Read before deploying")
        print("=" * 60)

        print("""
  -- GitHub Actions Setup --------------------------------------
  Go to your GitHub repo:
  Settings > Secrets > Actions > New repository secret

  Add these 2 secrets:
    DOCKER_USERNAME   ->  your Docker Hub username
    DOCKER_PASSWORD   ->  your Docker Hub password or token
""")
        print(f"  -- Docker Image Name ---------------------------------")
        print(f"  Your image will be pushed as:")
        print(f"    <DOCKER_USERNAME>/{project_name}:latest")
        print(f"""
  Make sure this name is available on hub.docker.com

  -- Kubernetes Ingress ----------------------------------------
  In k8s/ingress.yaml, replace:
    host: example.com
  With your actual domain:
    host: yourdomain.com

  -- Health Check Endpoint -------------------------------------
  Your app must expose this endpoint:
    GET /health  ->  returns 200 OK

  Example in FastAPI:
    @app.get("/health")
    def health():
        return {{"status": "ok"}}

  -- Environment Variables -------------------------------------
  Create a .env file in your project root before running:
    cp .env.example .env
  Or create it manually with your actual values.

  -- Deploy Order ----------------------------------------------
  1. Push code to GitHub main branch
       git push origin main

  2. GitHub Actions will automatically:
       - Build Docker image
       - Push to Docker Hub

  3. Deploy to Kubernetes:
       kubectl apply -f k8s/

  4. Check status:
       kubectl get pods
       kubectl get ingress
""")
        print("=" * 60)
        print(f"  Files saved at: {context.project_path}")
        print("=" * 60)
        print()