"""The operator's assistant: the agent that answers a question by calling the ops MCP server.

docs/enhancements/alexa-plus-operator-assistant-enhancement.md is the design (section 2). The
model decides which tools to call and which lead to follow; the code decides everything that must
be exact. The modules, from the one with the fewest dependencies:

- policy.py   the rules that need no model: which turn this is, which tools it may be offered,
              how many calls it may make, and what is collected for the page (plain Python)
- agent.py    the Strands agent on Bedrock, its system prompt, and its MCP client (the only
              module that imports `strands`)

The Lambda handler is ../ops_agent_handler.py, next to the other handlers.
"""
