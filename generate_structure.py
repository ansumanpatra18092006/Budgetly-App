from pathlib import Path

EXCLUDE = {
    "venv",
    ".git",
    "__pycache__",
    "node_modules",
    ".idea",
    ".vscode",
    "dist",
    "build",
    ".pytest_cache",
}

# Directories to skip completely (relative to project root)
EXCLUDE_PATHS = {
    Path("frontend/android"),
}

lines = []
ROOT = Path(".")


def build_tree(path, prefix=""):
    items = []

    for p in sorted(path.iterdir()):
        rel_path = p.relative_to(ROOT)

        # Skip excluded folder names
        if p.name in EXCLUDE:
            continue

        # Skip specific paths
        if rel_path in EXCLUDE_PATHS:
            continue

        items.append(p)

    for i, item in enumerate(items):
        connector = "├── " if i < len(items) - 1 else "└── "
        lines.append(prefix + connector + item.name)

        if item.is_dir():
            extension = "│   " if i < len(items) - 1 else "    "
            build_tree(item, prefix + extension)


lines.append(ROOT.resolve().name)
build_tree(ROOT)

with open("project_structure.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print("project_structure.txt created successfully!")