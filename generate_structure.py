from pathlib import Path

ROOT = Path(".").resolve()

# Directories to ignore anywhere
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    ".idea",
    ".vscode",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    ".gradle",
    "build",
    "dist",
}

# File extensions to ignore
EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".tmp",
    ".log",

    # Binary / generated files
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pkl",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".pdf",
    ".pptx",
    ".apk",
    ".aab",
    ".zip",
}

# Specific paths to ignore
EXCLUDE_PATHS = {
    Path("android/.gradle"),
}

lines = []


def should_exclude(path: Path) -> bool:
    rel = path.relative_to(ROOT)

    # Explicit path exclusions
    for excluded in EXCLUDE_PATHS:
        if rel == excluded or excluded in rel.parents:
            return True

    # Directory exclusions
    if path.is_dir() and path.name in EXCLUDE_DIRS:
        return True

    # Hidden folders/files
    if path.name.startswith(".") and path.name not in {
        ".env",
        ".gitignore",
        ".python-version",
    }:
        return True

    # File type exclusions
    if path.is_file() and path.suffix.lower() in EXCLUDE_EXTENSIONS:
        return True

    return False


def build_tree(path: Path, prefix: str = ""):
    try:
        children = sorted(
            [p for p in path.iterdir() if not should_exclude(p)],
            key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except PermissionError:
        return

    for i, child in enumerate(children):
        is_last = i == len(children) - 1

        connector = "└── " if is_last else "├── "
        lines.append(prefix + connector + child.name)

        if child.is_dir():
            extension = "    " if is_last else "│   "
            build_tree(child, prefix + extension)


lines.append(ROOT.name)
build_tree(ROOT)

output_file = ROOT / "project_structure.txt"

with output_file.open("w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"✓ Created: {output_file}")
print(f"✓ Total entries: {len(lines) - 1}")