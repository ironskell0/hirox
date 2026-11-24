# Contributing to Hirox

Thank you for your interest in contributing to Hirox!

## Getting Started

1. Fork the repository and clone your fork:
   ```bash
   git clone https://github.com/ironskell0/hirox.git
   cd hirox
   ```
2. Install in editable mode with test dependencies:
   ```bash
   pip install -e .
   pip install pytest
   ```
3. Run the test suite before making changes:
   ```bash
   pytest -q
   ```

## Development Guidelines

- Keep changes focused and atomic.
- Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Add or update tests in `tests/` for any new behavior or bug fix.
- Never commit API keys, tokens, `.env` files, or local cache artifacts.

## Submitting a Pull Request

1. Create a feature branch from `main` (e.g. `feat/my-feature`).
2. Ensure `pytest -q` passes with zero failures.
3. Open a Pull Request describing what changed and why.
