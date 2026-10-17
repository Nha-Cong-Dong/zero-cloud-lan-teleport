# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Teleport (Zero-Cloud LAN Teleport) is a desktop app, written in Python 3.12+, for sending very large files and syncing the clipboard between computers on the same LAN. There is no cloud, no accounts and no telemetry. It is built as a release-quality product. The current stage is early core work: framing, device identity and TLS. Discovery, pairing, transfer and the UI are not written yet.

The parent folder of `.claude/` is the git repo root (`git@github.com:Nha-Cong-Dong/zero-cloud-lan-teleport.git`, private). The repo root holds only `README.md` and `pyproject.toml`. Every other Markdown doc lives in `docs/`: `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `architecture.md`, `protocol.md`, plus `docs/README.md` as an index. Keep the root that tidy.

The internal design documents sit **outside the repo**, in `../tai_lieu/`:
- `ke_hoach.md`: architecture, protocol and roadmap.
- `mvp.md`: MVP feature spec with acceptance criteria.
- `y_tuong.md`: original idea.

Read them for design intent when they exist, but never link to them or copy them into repo files.

## Language

The user works in Vietnamese. Write docs, docstrings, comments and user-facing strings in Vietnamese. Identifiers stay in English. Ruff rules RUF001–003 are disabled for this reason.

## Commands

Run from this folder on Windows/PowerShell. The venv lives at `.venv/`.

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"                          # cryptography + pytest, pytest-asyncio, ruff
pytest                                           # all tests
pytest tests/test_tls.py::test_verify_pin_rejects_mismatch   # single test
ruff check . ; ruff format --check .             # CI runs exactly: ruff check, ruff format --check, pytest
teleport identity                                # CLI entry point (also: python -m teleport)
```

CI (`.github/workflows/ci.yml`) runs on Windows, macOS and Linux with Python 3.12 and 3.14. pytest uses `asyncio_mode = "auto"`, so a plain `async def test_...` works without decorators.

The distribution name is `zero-cloud-lan-teleport` and the import name is `teleport`. The version has one source, `__version__` in `src/teleport/__init__.py`; `pyproject.toml` reads it dynamically. Set `TELEPORT_HOME` (or pass `--config-dir`) to give each node its own identity when running several nodes on one machine.

## Architecture

Layering: `core ← net ← clipboard ← ui/cli`. A lower layer never imports an upper one, and `tests/test_architecture.py` enforces this by parsing imports, relative ones included. `paths.py` is a shared utility. `docs/architecture.md` holds the module map with each module's status. `docs/protocol.md` is the wire spec (protocol v1, draft), and any wire-format change must update it in the same PR. Create a module only when there is real code for it; don't add empty placeholder files.

- `src/teleport/core/`: protocol logic that knows nothing about the network.
  - `framing.py` defines the **control-channel** format: `[uint32 big-endian length][UTF-8 JSON object]`, capped at 16 MiB.
  - `read_message` returns `None` on a clean EOF at a frame boundary. A truncated or invalid frame raises `FramingError`.
  - Future message types (`HELLO`, `OFFER`, `RESUME`, `BLOCK_HASHES`…) ride on this framing. Bulk file data will go over a separate binary data channel.
- `src/teleport/net/identity.py`: each device has an ECDSA P-256 key and a self-signed cert stored in a config dir.
  - The device id is the cert's CN without its `teleport-` prefix.
  - The fingerprint is the SHA-256 of the cert in DER form. That fingerprint is the device's identity, and pairing means storing the peer's fingerprint.
- `src/teleport/net/tls.py`: TLS 1.3 only.
  - The client sets `CERT_NONE`, then pins by comparing the peer's fingerprint (`peer_fingerprint` + `verify_pin`, a constant-time prefix match of at least 16 hex chars).
  - Python's `ssl` has no custom verify callback. So the planned mutual auth on the server side is to load the trusted peers' certs as CA data. First-time pairing is instead authenticated at the application layer with a 6-digit code.

Planned design invariants from the roadmap:
- Transfer works in 1 MiB blocks. Each block is verified with SHA-256 and the file with a Merkle root.
- Resume uses a bitfield stored in a `.part.state` file. Incoming data is written to `.part` and renamed only after verification.
- Backpressure comes from `await writer.drain()` on the sender and a bounded write queue on the receiver, so RAM must not grow with file size.
- All peer input is untrusted. Received filenames must be sanitized (no `..`, no absolute paths, no Windows reserved names), and a received file never overwrites an existing one.

## Conventions

- Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). Changes users will notice go under `[Unreleased]` in `docs/CHANGELOG.md`.
- Version numbers live in two places: `pyproject.toml` and `src/teleport/__init__.py`. The network protocol is versioned separately from the app.
- Pick only dependencies under MIT/BSD/Apache/LGPL licenses. The project license is not decided yet.
