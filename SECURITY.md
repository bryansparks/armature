# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Armature, please report it privately rather than opening a public issue.

**Email:** bryan@elftech.ai

Include in your report:
- A description of the vulnerability
- Steps to reproduce the issue
- The potential impact
- Any suggested mitigations, if you have them

We will acknowledge your report within 3 business days and aim to resolve confirmed vulnerabilities within 14 days.

## Scope

Security issues relevant to Armature include:

- **Prompt injection** — attacks that cause an LLM stage to bypass safety rules or execute unintended tool calls
- **Safety rule bypass** — inputs or configurations that circumvent `ToolSafetyRule` enforcement
- **Sandbox escape** — vulnerabilities in the Docker sandbox provider that allow code to escape the container
- **Tool call injection** — malformed context values that cause `tool_call` stages to invoke unintended tools or arguments
- **Dependency vulnerabilities** — critical CVEs in core dependencies (litellm, pydantic, aiosqlite, typer)

## Out of Scope

- Workflows deliberately written to be unsafe by the operator
- LLM model hallucinations or unsafe outputs not caused by a harness defect
- Issues requiring physical access to the machine running Armature

## Security Model

Armature assumes the workflow spec author is trusted. The safety subsystem (`ToolSafetyRule`, `SafetyCondition`) is designed to constrain what **LLM-generated** tool calls can do at runtime — it is not a defense against a malicious spec author.

For production deployments, treat your workflow spec files as configuration that requires the same access controls as application code.
