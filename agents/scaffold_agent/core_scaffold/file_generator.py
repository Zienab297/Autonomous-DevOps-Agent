"""
core_scaffold/file_generator.py
--------------------------------
Takes LLM response and writes generated files to disk.
Handles multiple LLM output formats robustly.
"""

import re
from pathlib import Path

from agents.scaffold_agent.shared.models import GeneratedFile, ScaffoldResult


# ── known filenames the LLM might generate ───────────────────────────────────

KNOWN_FILENAMES = [
    "Dockerfile",
    ".dockerignore",
    "docker-compose.yml",
    ".github/workflows/deploy.yml",
    "k8s/deployment.yaml",
    "k8s/service.yaml",
    "k8s/ingress.yaml",
    "nginx.conf",
    ".env.example",
    "Makefile",
    "Jenkinsfile",
    ".gitlab-ci.yml",
    ".circleci/config.yml",
    "cloudbuild.yaml",
]


def parse_llm_response(response_text: str) -> list[GeneratedFile]:
    """
    Parse LLM response and extract generated files.

    Handles multiple formats the LLM might use:

    Format 1 (expected):
        FILE: Dockerfile
        ```
        FROM python...
        ```
        DESCRIPTION: ...

    Format 2 (numbered list with backtick filename):
        1. `Dockerfile` (TYPE A):
        ```
        FROM python...
        ```

    Format 3 (markdown header):
        ### Dockerfile
        ```
        FROM python...
        ```

    Format 4 (filename on its own line before code block):
        Dockerfile
        ```
        FROM python...
        ```
    """

    files = []

    # strategy 1: FILE: marker (expected format)
    files = _parse_file_marker(response_text)
    if files:
        print(f"[FileGenerator] Parsed using FILE: marker format")
        return files

    # strategy 2: numbered list with backtick filename
    files = _parse_numbered_list(response_text)
    if files:
        print(f"[FileGenerator] Parsed using numbered list format")
        return files

    # strategy 3: markdown header (### Filename)
    files = _parse_markdown_headers(response_text)
    if files:
        print(f"[FileGenerator] Parsed using markdown header format")
        return files

    # strategy 4: known filename before code block
    files = _parse_known_filenames(response_text)
    if files:
        print(f"[FileGenerator] Parsed using known filename format")
        return files

    print(f"[FileGenerator] WARNING: Could not parse any files from LLM response")
    return []


# ── parsing strategies ────────────────────────────────────────────────────────

def _parse_file_marker(text: str) -> list[GeneratedFile]:
    """Parse FILE: <filename> format."""
    files = []
    parts = re.split(r'FILE:\s*(.+?)(?:\n|$)', text)

    i = 1
    while i < len(parts):
        filename = parts[i].strip().strip('`').strip('"').strip("'")
        rest     = parts[i + 1] if i + 1 < len(parts) else ""

        content = _extract_code_block(rest)
        if not content:
            content = re.split(r'DESCRIPTION:|FILE:', rest)[0].strip()

        desc_match = re.search(r'DESCRIPTION:\s*(.+?)(?:\n|$)', rest)
        description = desc_match.group(1).strip() if desc_match else ""

        if filename and content:
            files.append(GeneratedFile(
                filename    = filename,
                content     = content,
                description = description,
            ))
        i += 2

    return files


def _parse_numbered_list(text: str) -> list[GeneratedFile]:
    """
    Parse numbered list format:
        1. `Dockerfile` (TYPE A):
        ```
        content
        ```
    """
    files = []

    # match: number. `filename` or number. filename
    pattern = re.compile(
        r'\d+\.\s+[`\'"]?([^\n`\'"(]+?)[`\'"]?\s*(?:\([^)]*\))?\s*:\s*\n'
        r'```(?:\w+)?\n(.*?)```',
        re.DOTALL
    )

    for match in pattern.finditer(text):
        filename = match.group(1).strip()
        content  = match.group(2).strip()

        # validate it looks like a real filename
        if _is_valid_filename(filename) and content:
            files.append(GeneratedFile(
                filename    = filename,
                content     = content,
                description = "",
            ))

    return files


def _parse_markdown_headers(text: str) -> list[GeneratedFile]:
    """
    Parse markdown header format:
        ### Dockerfile
        ```
        content
        ```
    """
    files = []

    pattern = re.compile(
        r'#{1,4}\s+([^\n]+)\n+```(?:\w+)?\n(.*?)```',
        re.DOTALL
    )

    for match in pattern.finditer(text):
        filename = match.group(1).strip().strip('`').strip('"').strip("'")
        content  = match.group(2).strip()

        if _is_valid_filename(filename) and content:
            files.append(GeneratedFile(
                filename    = filename,
                content     = content,
                description = "",
            ))

    return files


def _parse_known_filenames(text: str) -> list[GeneratedFile]:
    """
    Parse by searching for known filenames followed by a code block.
    Last resort fallback.
    """
    files = []

    for known in KNOWN_FILENAMES:
        # escape special chars for regex (e.g. dots in filenames)
        escaped = re.escape(known)

        pattern = re.compile(
            rf'{escaped}\s*\n+```(?:\w+)?\n(.*?)```',
            re.DOTALL
        )

        match = pattern.search(text)
        if match:
            content = match.group(1).strip()
            if content:
                files.append(GeneratedFile(
                    filename    = known,
                    content     = content,
                    description = "",
                ))

    return files


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_code_block(text: str) -> str:
    """Extract content from first code block found."""
    match = re.search(r'```(?:\w+)?\n(.*?)```', text, re.DOTALL)
    return match.group(1).strip() if match else ""


def _is_valid_filename(name: str) -> bool:
    """Check if string looks like a real filename."""
    name = name.strip()

    # must contain a dot or be a known name without extension
    known_no_ext = {"Dockerfile", "Makefile", "Jenkinsfile"}
    if name in known_no_ext:
        return True

    # reject if too long or contains newlines
    if len(name) > 60 or "\n" in name:
        return False

    # must look like a filename (has extension or path separator)
    if "." in name or "/" in name:
        return True

    return False


# ── file writer ───────────────────────────────────────────────────────────────

def write_files(result: ScaffoldResult, dry_run: bool = False) -> list[str]:
    """
    Write all generated files to the project directory.

    Args:
        result:  ScaffoldResult with generated files
        dry_run: If True, print files but do not write them

    Returns:
        List of written file paths
    """
    written = []
    base    = Path(result.project_path)

    for gf in result.generated_files:
        filepath = base / gf.filename

        if dry_run:
            print(f"[FileGenerator] (dry-run) Would write: {gf.filename}")
            if gf.description:
                print(f"  {gf.description}")
            written.append(str(filepath))
            continue

        # create parent directories if needed
        filepath.parent.mkdir(parents=True, exist_ok=True)

        filepath.write_text(gf.content, encoding="utf-8")
        print(f"[FileGenerator] Written: {gf.filename}")
        if gf.description:
            print(f"  {gf.description}")

        written.append(str(filepath))

    return written