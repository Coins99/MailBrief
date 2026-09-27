"""PyInstaller entry point (no source-checkout imports or data paths)."""

from mailbrief.app import main

raise SystemExit(main())
