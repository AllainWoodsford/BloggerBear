"""The operator's assistant: an MCP server over the pipeline's own data, read-only.

docs/enhancements/alexa-plus-operator-assistant-enhancement.md is the design. An agent asks these
tools what needs the operator's attention; each answer carries a short `spoken` text, the data
behind it, and `findings` with the admin_cli command that would fix each one. Nothing here changes
the pipeline, and nothing here runs a command: the operator does, after reading it.

- tools.py        what each tool reads and returns (plain functions, no MCP in them): the
                  pipeline's own state, and the helpers the next two share
- content.py      content_checks: published articles and musings that look wrong
- account.py      security_events, alarms and spend
- suggestions.py  the fixed catalogue of commands a finding can suggest
- memory.py       what it has suggested and what it is watching: the one table it writes to
- access.py       the assistant_access switch, checked on every request
- server.py       the MCP server and its web app (the only module that needs the `mcp` package)
"""
