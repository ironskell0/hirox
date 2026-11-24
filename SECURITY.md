# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability in Hirox, please **do not** open a public GitHub issue.

Instead, report it privately using **GitHub Security Advisories** on the repository:
`https://github.com/ironskell0/hirox/security/advisories/new`

Please include:
- A clear description of the vulnerability and potential impact
- Steps to reproduce or a minimal proof of concept
- Any suggested remediation if available

We will acknowledge receipt within 48 hours and work with you on a coordinated fix and disclosure.

## Security Best Practices for Users

- **API Keys & OAuth Tokens**: Store credentials in environment variables (e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`) or local `~/.hirox/` config files with restricted permissions. Never commit `.env` files or tokens to version control.
- **Memory Redaction**: Hirox automatically redacts common secret patterns (such as `sk-...`, `AIza...`, `ghp_...`, `xox...`) before persisting entries to memory.
- **Tool Execution**: Review tool permissions and run `hirox doctor` to verify your environment and gateway configuration before exposing any gateway beyond `127.0.0.1`.
