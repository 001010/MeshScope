"""Frozen-executable entry point (cx_Freeze needs a top-level script, not a package module)."""

from meshscope.app import main

if __name__ == "__main__":
    main()
