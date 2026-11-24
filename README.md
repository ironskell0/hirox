# Hirox

> Python-based multi-gateway AI agent foundation with an interactive terminal TUI, multi-provider support, persistent memory, and built-in diagnostics.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Security Policy](https://img.shields.io/badge/security-policy-green.svg)](SECURITY.md)

<p align="center">
  <img src="assets/hirox-demo.gif" alt="Hirox CLI and TUI Demo" width="800" />
</p>

## Features

- **Multi-Gateway Runtime**: Run and manage multiple agent gateways from a unified runtime (`hirox start`).
- **Interactive Chat & Config TUI**: Rich terminal chat interface (`hirox chat`) and interactive configuration manager (`hirox config`).
- **Multi-Provider Catalog**: Supports OpenAI, Anthropic, Google Gemini, OpenRouter, Grok, and OAuth-backed provider flows.
- **Persistent Memory with Secret Redaction**: Stores session context while automatically redacting common API key and token patterns.
- **Built-in Diagnostics**: Verify Python environment, configuration, providers, and gateways in one command (`hirox doctor`).

## Quick Start

**Prerequisites**: Python 3.11+

```bash
git clone https://github.com/ironskell0/hirox.git
cd hirox
pip install -e .
```

Initialize configuration and run diagnostics:

```bash
hirox config init
hirox doctor
```

Launch the interactive chat TUI:

```bash
hirox chat
```

## CLI Commands

| Command | Description |
| --- | --- |
| `hirox start` | Start the Hirox agent runtime and enabled gateways (`--dry-run`, `--gateway`, `--port`) |
| `hirox chat` | Launch the interactive terminal chat TUI (`--gateway`, `--tui/--no-tui`) |
| `hirox config` | Open the interactive Config TUI, or use `init`, `show`, `get`, `set`, `path` |
| `hirox doctor` | Run environment, config, and gateway diagnostics (`--json`) |

## Security

Hirox is designed with local-first credential hygiene and automatic secret redaction in memory storage.
Please review [SECURITY.md](SECURITY.md) for best practices and private vulnerability reporting instructions.

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

## License

MIT © [ironskell0](https://github.com/ironskell0) — see [LICENSE](LICENSE) for details.
