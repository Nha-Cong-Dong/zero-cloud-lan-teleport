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
pytest -m "not integration"                      # skip tests that open real sockets on 127.0.0.1
pytest tests/net/test_tls.py::test_verify_pin_rejects_mismatch   # single test
ruff check . ; ruff format --check .             # CI runs exactly: ruff check, ruff format --check, pytest
teleport identity                                # CLI entry point (also: python -m teleport)
```

CI (`.github/workflows/ci.yml`) runs on Windows, macOS and Linux with Python 3.12 and 3.14. pytest uses `asyncio_mode = "auto"` (a plain `async def test_...` works) and `--import-mode=importlib`. Tests mirror the layers (`tests/core/`, `tests/trust/`, `tests/net/`), and socket tests carry `@pytest.mark.integration`.

The distribution name is `zero-cloud-lan-teleport` and the import name is `teleport`. The version has one source, `__version__` in `src/teleport/__init__.py`; `pyproject.toml` reads it dynamically. Set `TELEPORT_HOME` (or pass `--config-dir`) to give each node its own identity when running several nodes on one machine.

## Architecture

Layering: `core ← trust ← net ← app ← ui / cli`. `tests/test_architecture.py` parses every import, relative ones included, and enforces these rules:
- A layer imports only the layers below it. `ui` and `cli` never import each other.
- `core` is **sans-IO**. It must not import `asyncio`, `socket`, `ssl`, `threading`, `zeroconf`, `PySide6`, `teleport.paths` or any other layer.
- Every top-level `teleport.*` module belongs to a layer. A new package must be added to `LAYERS` in that test and to `docs/architecture.md`.

`paths.py` is a shared utility that any layer except `core` may use. `docs/architecture.md` is the **single source** for module status and the process model. `docs/protocol.md` is the wire spec (protocol v1, draft), and any wire-format change must update it in the same PR. Create a module only when there is real code for it; don't add empty placeholder files.

What lives where:
- **core**: pure functions and state machines. `framing.py` encodes and decodes control frames: `[uint32 BE length][UTF-8 JSON object]`, capped at 16 MiB.
  - `messages.py` holds frozen dataclasses for each message type. Validation runs in `__post_init__`, and `parse_message` and `to_dict` sit on top. This is the single place where untrusted peer fields are checked, using ASCII-only `re.fullmatch` checks.
  - `pairing_code.py` computes the 6-digit code from a commitment plus two nonces (`docs/protocol.md` §5). Nonce generation lives in `app`, not here.
  - Transfer and resume logic goes here as sans-IO state machines: events in, actions out. That keeps the hard logic testable with hypothesis and no sockets.
  - Transfer logic does **not** belong in `net`.
- **trust**: `identity.py` holds the device's ECDSA P-256 key and self-signed cert. The fingerprint is the SHA-256 of the DER cert, and the device id is the CN without its `teleport-` prefix.
  - `peer_cert_from_pem` and `peer_cert_from_der` strictly validate a peer's cert, because every trusted cert becomes an mTLS trust anchor. Anything that could act as a CA is rejected.
  - `store.py` is the `TrustStore` for `trusted.json`. Writes are atomic, and a corrupt file raises `TrustStoreError` and is never rewritten. Build it with `TrustStore.load()` only.
  - Pairing exchanges and stores the peer's cert PEM (`Identity.cert_pem()`).
- **net**: only I/O, no business decisions.
  - `lock.py`: `NodeLock` holds `node.lock` (`msvcrt` on Windows, `flock` on POSIX), giving one node per config dir.
  - `stream.py` reads and writes frames on asyncio streams. `read_message` returns `None` on a clean EOF at a frame boundary.
  - `tls.py`: the client pins the server's fingerprint (`CERT_NONE` + `verify_pin`).
  - Server-side mTLS comes from `server_context(identity, trusted_certs=[pem…])`, which sets `CERT_REQUIRED` and loads the trusted certs as anchors. Unknown clients fail the handshake; this is verified in `tests/net/test_tls.py`.
  - `trusted_certs=None` means no client auth, and is used only by the pairing listener.
  - Under TLS 1.3 a rejected client still sees the connect succeed and only fails on its first read. So a connection counts as established only after the server's `hello` has been read.
- **app** (planned): `Node` plus the services (pairing, transfers, clipboard). This is the only orchestration layer, shared by the CLI and the UI.
  - Clipboard is a feature here, not a layer. It works through a `ClipboardBackend` interface; the UI supplies the Qt version.

Process model: one node per config dir. Up to `v0.2`, each CLI run is its own node and holds `node.lock`. From `v0.3`, the GUI hosts the node and the CLI talks to it over local IPC.

Planned design invariants from the roadmap:
- Transfer works in 1 MiB blocks. Each block is verified with SHA-256 and the file with a Merkle root.
- Resume uses a bitfield stored in a `.part.state` file. Incoming data is written to `.part` and renamed only after verification.
- Backpressure comes from `await writer.drain()` on the sender and a bounded write queue on the receiver, so RAM must not grow with file size.
- All peer input is untrusted. Received filenames must be sanitized (no `..`, no absolute paths, no Windows reserved names), and a received file never overwrites an existing one.

## Team of subagents

`.claude/agents/` defines six project subagents. Their prompts are in Vietnamese.

| Agent | Role | Edits files? |
|---|---|---|
| `truong-nhom` | Lead. Plans the work (design, task table, done criteria), then gives the final ĐẠT / CHƯA ĐẠT verdict | No |
| `hoi-lai` | Clarifier. Finds ambiguity, conflicts and risks, and drafts questions for the user with suggested defaults | No |
| `lap-trinh` | Coder. Implements one assigned task in `src/` and runs ruff + pytest. Never commits | Yes |
| `viet-test` | Test writer. Writes tests in `tests/` from the spec, not from the code; never edits `src/` | Yes, `tests/` only |
| `review` | Reviewer. Reviews the uncommitted diff for correctness, security, layering and docs, with severity levels | No |
| `tom-tat` | Summarizer. Summarizes verified changes and drafts the CHANGELOG entry and commit message | No |

Subagents cannot call each other, so **the main session orchestrates**. For any non-trivial feature, run this pipeline:
1. `truong-nhom` makes the plan.
2. If the plan has open questions, run `hoi-lai`, then ask the user the blocking questions.
3. Run `lap-trinh` and `viet-test` in parallel, from the plan's API and done criteria.
4. Loop `review` and fixes until no **nghiêm trọng** or **cao** issues remain.
5. `truong-nhom` gives the verdict, fed with the review and test results.
6. `tom-tat` writes the summary and commit message.

Commit and push only after the user approves. Small fixes of a few lines can skip the pipeline.

## Conventions

- Commits follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`). Changes users will notice go under `[Unreleased]` in `docs/CHANGELOG.md`.
- The app version lives only in `src/teleport/__init__.py`. The network protocol is versioned separately, in `docs/protocol.md`.
- Runtime or bundled dependencies must be MIT/BSD/Apache/LGPL. Dev-only tools may use other licenses: `hypothesis` (MPL-2.0) is approved. The project license is not decided yet.
