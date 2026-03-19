"""
test_scaffold_agent.py
-----------------------
End-to-end test for the Scaffold Agent.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.config import load_config
from core_scaffold.scaffold_agent import ScaffoldAgent


def print_separator(char="─", width=60):
    print(char * width)


def main():
    print_separator("=")
    print("  Scaffold Agent -- End-to-End Test")
    print_separator("=")

    # project path -- reads existing project and generates deployment files
    project_path = r"C:\Users\asus\Downloads\Hoda\Track\Agent\scaffold_test"

    print(f"\n[INFO] Reading project from: {project_path}")

    # load config and run scaffold agent
    config = load_config()
    agent  = ScaffoldAgent(config)

    print("\n[TEST] Running ScaffoldAgent (dry_run=False)...")
    result = agent.run(project_path, dry_run=False)

    # print summary
    print_separator()
    print(f"\n  language      : {result.language.value}")
    print(f"  framework     : {result.framework.value}")
    print(f"  files         : {len(result.generated_files)}")
    print()

    passed = 0
    failed = 0

    # expected files based on what scanner detected
    expected_always = [
        "Dockerfile",
        "docker-compose.yml",
        ".dockerignore",
        ".github/workflows/deploy.yml",
    ]

    expected_k8s = [
        "k8s/deployment.yaml",
        "k8s/service.yaml",
        "k8s/ingress.yaml",
    ]

    generated_names = [f.filename for f in result.generated_files]

    # verify always-required files
    for expected in expected_always:
        if any(expected in name for name in generated_names):
            print(f"  PASSED -- {expected} generated")
            passed += 1
        else:
            print(f"  FAILED -- {expected} missing")
            failed += 1

    # verify k8s files only if kubernetes was detected
    from shared.models import DeployTarget
    # re-scan to check deploy target
    from core_scaffold.project_scanner import ProjectScanner
    scanner = ProjectScanner(config)
    context = scanner.scan(project_path)

    if context.deploy_config.deploy_target == DeployTarget.KUBERNETES:
        for expected in expected_k8s:
            if any(expected in name for name in generated_names):
                print(f"  PASSED -- {expected} generated")
                passed += 1
            else:
                print(f"  FAILED -- {expected} missing")
                failed += 1
    else:
        print(f"  INFO  -- k8s files skipped (deploy target: docker only)")

    total = passed + failed
    print()
    print_separator("=")
    print(f"  Results: {passed} passed / {failed} failed / {total} total")
    print_separator("=")

    if failed == 0:
        print("\n  All tests passed\n")
    else:
        print(f"\n  {failed} file(s) missing\n")

    # preview generated file contents
    print_separator()
    print("  Generated files preview:")
    print_separator()
    for gf in result.generated_files:
        print(f"\n  -- {gf.filename} --")
        if gf.description:
            print(f"  {gf.description}")
        print()
        for line in gf.content.splitlines()[:10]:
            print(f"    {line}")
        if len(gf.content.splitlines()) > 10:
            print(f"    ... ({len(gf.content.splitlines())} lines total)")

    print(f"\n  Files saved at: {project_path}")


if __name__ == "__main__":
    main()