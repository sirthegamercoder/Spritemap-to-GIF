import sys
import shutil
from pathlib import Path
from PyInstaller.__main__ import run

root = Path(__file__).parent.absolute()
dist = root / "dist"


def platform_name():
    if sys.platform == "win32":
        return "Spritemap to GIF Windows"
    elif sys.platform == "darwin":
        return "Spritemap to GIF MacOS"
    else:
        return "Spritemap to GIF Linux"


def main():
    base_args = [
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name=Spritemap to GIF",
        "--clean",
    ]

    if sys.platform == "win32":
        base_args.append(f'--icon={root / "assets" / "icon.ico"}')
    else:
        pass

    if sys.platform == "win32":
        base_args.append(f'--add-data={root / "assets"};assets/')
    else:
        base_args.append(f'--add-data={root / "assets"}:assets/')

    if sys.platform == "win32":
        base_args.append(
            f'--add-data={root / ".venv" / "Lib" / "site-packages" / "customtkinter"};customtkinter/'
        )
    else:
        base_args.append(
            f'--add-data={root / ".venv" / "Lib" / "site-packages" / "customtkinter"}:customtkinter/'
        )

    base_args.append(str(root / "src" / "Main.py"))
    run(base_args)

    archive_base = dist / platform_name
    shutil.make_archive(str(archive_base), "zip", dist)


if __name__ == "__main__":
    main()
