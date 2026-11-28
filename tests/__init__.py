"""ProjectFlow's own test package.

Avoid importing an unrelated installed `tests.conftest` package from
Terminal MCP or other development tooling when regression tests use
`from tests.conftest import ...`.
"""
