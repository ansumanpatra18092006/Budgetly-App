from pathlib import Path

# Directory names to exclude anywhere in the project tree
EXCLUDE_DIRS = {
    ".venv",
    "venv",
    "env",
    ".git",
    "__pycache__",
    "node_modules",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
}

# Specific relative paths to exclude
EXCLUDE_PATHS = {
    Path("frontend/android"),
}

# File extensions to exclude from the generated structure
EXCLUDE_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".tmp",
    ".log",
}

ROOT = Path(".").resolve()
lines = []


def should_exclude(path: Path) -> bool:
    """Return True when a file/folder should not appear in the tree."""
    rel_path = path.relative_to(ROOT)

    # Explicit path exclusion
    if rel_path in EXCLUDE_PATHS:
        return True

    # Directory exclusion
    if path.is_dir() and path.name in EXCLUDE_DIRS:
        return True

    # File extension exclusion
    if path.is_file() and path.suffix.lower() in EXCLUDE_EXTENSIONS:
        return True

    return False


def build_tree(path: Path, prefix: str = ""):
    items = []

    try:
        children = sorted(
            path.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except PermissionError:
        return

    for item in children:
        if should_exclude(item):
            continue
        items.append(item)

    for index, item in enumerate(items):
        is_last = index == len(items) - 1
        connector = "└── " if is_last else "├── "

        lines.append(prefix + connector + item.name)

        if item.is_dir():
            extension = "    " if is_last else "│   "
            build_tree(item, prefix + extension)


# Root name
lines.append(ROOT.name)

# Build tree
build_tree(ROOT)

# Write output
output_file = ROOT / "project_structure.txt"

with output_file.open("w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"{output_file.name} created successfully!")