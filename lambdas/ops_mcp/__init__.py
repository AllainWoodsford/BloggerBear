"""The operator's assistant: an MCP server over the pipeline's own data, read-only.

docs/enhancements/alexa-plus-operator-assistant-enhancement.md is the design. An agent asks these
tools what needs the operator's attention; each answer carries a short `spoken` text, the data
behind it, and `findings` with the admin_cli command that would fix each one. Nothing here changes
the pipeline, and nothing here runs a command: the operator does, after reading it.

- tools.py        what each tool reads and returns (plain functions, no MCP in them)
- suggestions.py  the fixed catalogue of commands a finding can suggest
- server.py       the MCP server and its web app (the only module that needs the `mcp` package)
"""
