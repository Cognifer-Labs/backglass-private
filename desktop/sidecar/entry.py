"""PyInstaller entry point. The frozen binary IS the full CLI — `backglass-server
dashboard` serves the app, and launchd jobs can call `sync`/`brief`/`plan` on the
same binary, so the packaged product needs no Python install at all."""

from backglass.__main__ import main

main()
