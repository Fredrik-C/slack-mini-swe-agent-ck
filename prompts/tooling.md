# Runtime and Tooling Guide

## Language build/test capability
This runtime supports:
- .NET SDKs: 8.x, 9.x, 10.x
- Python: `python3`, `pip3`
- Node.js + npm + TypeScript CLI (`tsc`)

When validating changes, run language-appropriate commands from the target repo:
- .NET: `dotnet --list-sdks`, `dotnet restore`, `dotnet build`, `dotnet test`
- Python: create/use venv when needed, install deps, then run `pytest` or project test command
- TypeScript: install deps (`npm ci` / `pnpm i` / `yarn`), then run `npm run build`, `npm test`, and/or `npx tsc --noEmit`

Prefer repo-native scripts when they exist (`npm test`, `poetry run pytest`, `dotnet test <sln/csproj>`).

## Context King protocol (mandatory for source discovery)
Use Context King for source navigation before broad grep/find on large repositories.

Command prefix options:
- `ck`
- `~/.ck/bin/ck` (Linux/macOS fallback)
- `%USERPROFILE%\\.ck\\bin\\ck.exe` (Windows fallback)

Required navigation sequence for source files (`.cs`, `.ts`, `.tsx`):
1. `ck get-keyword-map --query "<domain area concept operation>"`
2. `ck find-files --query "<same query>" [--path <root>]`
3. Confirm a folder, then run `ck recall --folder <folder>` before reading method bodies
4. Use targeted reads (`ck find-symbol`, `ck refs`, `ck signatures`, `ck get-method-source`, `ck get-type-source`, `ck read-full-file` only when necessary)
5. Edit
6. If CK tools were used this session, run `ck learn` before finalizing

Rules:
- Do not start repo-wide grep/glob/find for source files before CK scope is established.
- Keep grep/rg constrained to CK-confirmed folders.
- Do not re-run identical CK discovery commands; refine query/patterns instead.
- If CK is unavailable in the current environment, report that explicitly and fall back to scoped `rg` usage.
