# {{ title }} - Tier 1 Autonomous Agent

## Overview

{{ description }}

**Problem Statement**: {{ problem_statement }}

**Target Audience**: {{ target_audience }}

---

## Persona Source

- **Academy persona**: {{ persona_id }}
- **Persona YAML path**: `personas/{{ persona_id }}/persona.yaml`
- **Agent config model**: {{ model }}
- **Tool groups**: {{ tool_groups | join(', ') if tool_groups else 'file_readonly' }}
- **Prompt file**: `prompts/{{ prompt_file }}`
- **Promotion reason**: {{ promotion_reason }}

---

## Tech Stack

- **Language**: Python 3.11+
- **Agent Framework**: Claude Agent SDK (`anthropic-agent-sdk`)
- **LLM**: Claude via Anthropic API (model: {{ model }})
- **Configuration**: YAML persona definitions (loaded via yaml_loader.py pattern)
- **Package Manager**: pip with requirements.txt
- **Testing**: pytest
- **Type Checking**: mypy (optional)

---

## Environment Setup

### Prerequisites
- Python 3.11+
- Access to Anthropic API

### Configuration

Environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `ACADEMY_DIR` | No | Path to Academy personas directory (default: ../agent-persona-academy/personas) |
| `DEBUG` | No | Enable debug logging (default: false) |

---

## Architecture

```
+-------------------------------------------------------+
|              {{ title }}                               |
|                                                        |
|  +------------------+    +------------------------+   |
|  | Persona Config   |    | Agent Core (SDK)       |   |
|  | (persona.yaml)   |--->| - System prompt        |   |
|  | - voice          |    | - Tool resolution      |   |
|  | - frameworks     |    | - Model: {{ model }}   |   |
|  | - agent_config   |    | - Guardrails (hooks)   |   |
|  +------------------+    +------------------------+   |
|                                |                       |
|                          +-----v-----+                 |
|                          |   Tools   |                 |
|                          | - Read    |                 |
|                          | - Glob    |                 |
|                          | - Grep    |                 |
|                          | - Bash*   |                 |
|                          +-----------+                 |
|                                                        |
|  +------------------+    +------------------------+   |
|  | Guardrail Hooks  |    | Output                 |   |
|  | - Bash allowlist |    | - Structured results   |   |
|  | - Read-only mode |    | - Audit log            |   |
|  | - Path restrict  |    +------------------------+   |
|  +------------------+                                  |
+-------------------------------------------------------+
```

*Bash tool availability depends on guardrail configuration.

---

## Core Features

### Feature 1: Persona YAML Loading
**Description**: Load persona definition from Academy YAML and configure agent

**Requirements**:
- Parse persona.yaml using PyYAML
- Extract identity, voice, frameworks, and agent_config sections
- Generate system prompt from persona voice characteristics and frameworks
- Resolve tool groups from agent_config.tools.groups
- Apply model selection from agent_config.model

**Test Steps**:
1. Load persona YAML -> all sections parsed correctly
2. Generate system prompt -> includes persona voice and frameworks
3. Resolve tool groups -> correct tools available
4. Missing persona YAML -> clear error message

---

### Feature 2: Agent SDK Integration
**Description**: Initialize and run Claude Agent SDK agent with persona configuration

**Requirements**:
- Create Agent instance with persona-derived system prompt
- Configure model from agent_config.model ({{ model }})
- Register resolved tools from tool group catalog
- Support max_turns from agent_config (default: 10)
- Handle API errors gracefully

**Test Steps**:
1. Initialize agent -> connects to Claude API
2. Submit task -> agent processes with persona voice
3. API error -> agent fails gracefully with clear message
4. Max turns reached -> agent stops cleanly

---

### Feature 3: Guardrail Hooks
**Description**: Implement safety guardrails as Agent SDK hooks

**Requirements**:
- PreToolUse hook for Bash command allowlist (if Bash tool enabled)
- PreToolUse hook for read-only enforcement (if guardrails.read_only = true)
- PreToolUse hook for path restrictions (if guardrails.allowed_paths set)
- PostToolUse hook for output sanitization
- All hooks log decisions for audit trail

**Test Steps**:
1. Bash allowlist -> blocks disallowed commands, allows safe ones
2. Read-only mode -> blocks Write/Edit tools
3. Path restriction -> blocks access outside allowed paths
4. Hook logging -> all decisions recorded

---

### Feature 4: CLI Entry Point
**Description**: Command-line interface for running the agent

**Requirements**:
- Accept task description as argument or from stdin
- Support --persona flag to specify persona ID
- Support --model flag to override model
- Support --max-turns flag to limit execution
- Support --dry-run flag for testing without API calls
- Output results to stdout in structured format

**Test Steps**:
1. Run with task argument -> agent executes task
2. Run with --dry-run -> shows configuration without API call
3. Run with --help -> displays usage information
4. Missing required args -> clear error message

---

### Feature 5: Testing Suite
**Description**: Comprehensive pytest test coverage

**Requirements**:
- Unit tests for persona YAML loading
- Unit tests for tool group resolution
- Unit tests for guardrail hooks (mock tool calls)
- Integration test for agent initialization
- Test persona voice fidelity (output matches persona markers)

**Test Steps**:
1. Run pytest -> all tests pass
2. Tests cover persona loading, tool resolution, guardrails
3. Tests verify persona voice markers in output
4. Tests handle missing/invalid persona YAML

---

## Data Models

```python
from pydantic import BaseModel
from typing import Literal, Optional
from datetime import datetime


class PersonaConfig(BaseModel):
    """Loaded from persona.yaml agent_config section."""
    description: str
    prompt_file: str
    model: Literal["haiku", "sonnet", "opus"] = "sonnet"
    tool_groups: list[str] = ["file_readonly"]
    read_only: bool = False
    max_turns: int = 10


class AgentTask(BaseModel):
    """Task submitted to the agent."""
    id: str
    description: str
    persona_id: str = "{{ persona_id }}"
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    created_at: datetime
    completed_at: Optional[datetime] = None


class AgentResult(BaseModel):
    """Result from agent execution."""
    task_id: str
    persona_id: str
    output: str
    tool_calls: int = 0
    turns_used: int = 0
    model: str = "{{ model }}"
    duration_seconds: float = 0.0
```

---

## File Structure

```
{{ title | lower | replace(" ", "-") }}/
+-- agent/
|   +-- __init__.py
|   +-- main.py              # Entry point, Agent SDK setup
|   +-- config.py            # Persona YAML loading, tool resolution
|   +-- hooks.py             # Guardrail hooks (bash security, read-only, etc.)
|   +-- tools.py             # Custom tool implementations (if any)
|   +-- prompts.py           # System prompt generation from persona
+-- persona/
|   +-- persona.yaml         # Copy of Academy persona definition
+-- tests/
|   +-- __init__.py
|   +-- test_config.py       # Persona loading tests
|   +-- test_hooks.py        # Guardrail hook tests
|   +-- test_prompts.py      # System prompt generation tests
+-- .env.example
+-- requirements.txt
+-- README.md
+-- init.sh                  # Startup script
```

---

## Success Criteria

1. Agent loads persona YAML and generates correct system prompt
2. Tool groups resolve to correct SDK tool definitions
3. Guardrail hooks enforce read-only / bash allowlist constraints
4. Agent responds in persona voice (passes fidelity check markers)
5. CLI accepts tasks and produces structured output
6. All tests pass with good coverage
7. Agent can be started via init.sh
8. Agent respects max_turns limit

---

## Constraints & Notes

- This agent MUST use the Claude Agent SDK (anthropic-agent-sdk), not raw API calls
- System prompt MUST be derived from persona YAML voice/frameworks, not hardcoded
- Tool resolution MUST use the shared tool group catalog pattern from Academy
- Guardrail hooks are REQUIRED -- no agent ships without at least read-only or bash allowlist
- The agent should be runnable standalone or as a subagent within yce-harness
- All API keys loaded from environment variables, never hardcoded
- Follow the existing yce-harness agent patterns for consistency
