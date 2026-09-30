"""Package extension/ into claude-for-libreoffice.oxt (an .oxt is a zip)."""

import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "extension")
OUT = os.path.join(HERE, "claude-for-libreoffice.oxt")


def build(out=OUT):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(SRC):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                path = os.path.join(root, name)
                z.write(path, os.path.relpath(path, SRC).replace(os.sep, "/"))
    return out


if __name__ == "__main__":
    print(build())
