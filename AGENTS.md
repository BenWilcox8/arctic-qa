# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Read `docs/CORPUS_VIEWER.md` before you operate the read-only corpus-stage monitor.
- Read `docs/PUBLICATION_EXPORT.md` before you operate the live publication snapshot service.
- Read `docs/SOURCE_SCREENING_PASS.md` before you operate a bounded source pass.
- Run the tests as `nix develop -c bash -c 'PYTHONPATH=src pytest tests/<file>'`. The devshell does not install the package, so a bare `pytest` fails to import `arctic_qa`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
