# Contributing

Read [AGENTS.md](AGENTS.md) for architecture, invariants, and validation commands.
Open an issue for substantial changes; small fixes can go directly to a pull request.

Include the problem, resulting behavior, and tests run in your PR. Use synthetic
post/media fixtures and mocked requests. Never upload personal vaults or credentials.

The project uses Python 3.11+ with no runtime dependencies. Run:

```sh
python3 -m unittest discover -s tests -v
python3 -m x_bookmarks --help
```

Keep README examples and CLI help in sync. Changes to deletion or partial exports
must explicitly describe their effect on saved files and bookmark retention.
