# Examples

| File | What it shows |
|---|---|
| `claude-code/.mcp.json` | Attach Substrate to Claude Code as an MCP server (stdio per agent, or the shared HTTP daemon). |
| `claude-code/settings.json` | Hooks: inject the brief at `SessionStart`, store an episodic summary at `Stop`. |
| `multi_agent_handoff.py` | Two MCP clients on one daemon: lease conflict, handoff with a note, resume, episodic memory. |

## Session two remembers session one

```bash
pip install substrate-agent
cd your-project
substrate doctor                 # verifies install, prints the MCP config snippet
substrate mcp-config > .mcp.json # or merge into an existing one
cp examples/claude-code/settings.json .claude/settings.json
```

Session 1 in Claude Code: *"Remember: we deploy with `make release`, never push to main."*
The agent calls `memory.remember`. Session 2 starts with that rule already in
context, because the `SessionStart` hook ran `substrate hook session-start`.

Run the handoff demo:

```bash
python examples/multi_agent_handoff.py
```
