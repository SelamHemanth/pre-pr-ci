#!/usr/bin/env python3
import io
import sys

def update_file(path):
    with io.open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Perform replacements
    content = content.replace(
        "string.split(in_line, sep='\\t')",
        "in_line.split('\\t')"
    )
    content = content.replace(
        "string.split(kabi[symbol], sep='\\t')",
        "kabi[symbol].split('\\t')"
    )
    content = content.replace(
        "string.split(symvers[symbol], sep='\\t')",
        "symvers[symbol].split('\\t')"
    )
    content = content.replace(
        "symvers.has_key(symbol):",
        "symbol in symvers:"
    )

    with io.open(path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Updated {path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: update-kabi.py <path_to_check-kabi>")
        sys.exit(1)
    update_file(sys.argv[1])
