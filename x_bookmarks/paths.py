from pathlib import Path


def assets_directory(vault: Path, output: Path, folder=None):
    """Explicit asset folders are vault-relative; defaults remain beside notes."""
    vault = vault.resolve()
    path = (vault / folder).resolve() if folder is not None else (output / "assets").resolve()
    if folder is not None and (not folder.strip() or Path(folder).is_absolute()):
        raise ValueError("--assets-folder must be a nonempty relative path within the vault")
    if not path.is_relative_to(vault):
        raise ValueError("--assets-folder must remain inside the vault (including symlinks)")
    if any(part in {".x-bookmarks", ".obsidian"} for part in path.relative_to(vault).parts):
        raise ValueError("--assets-folder must not use export state or Obsidian configuration directories")
    return path
