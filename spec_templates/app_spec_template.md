# {{ title }} - App Specification

## Overview

{{ description }}

**Problem Statement**: {{ problem_statement }}

**Target Audience**: {{ target_audience }}

---

## Tech Stack

{% if artifact_type == "tool" %}
- **Language**: Python 3.11+
- **Package Manager**: uv or pip
- **CLI Framework**: argparse or click
- **Testing**: pytest
- **Type Checking**: mypy (optional)
{% elif artifact_type == "agent" %}
- **Language**: Python 3.11+
- **Package Manager**: uv or pip
- **Agent Framework**: LangChain or custom
- **LLM/AI**: Claude via Anthropic API or OpenRouter
- **Testing**: pytest
- **Type Checking**: mypy (optional)
{% elif artifact_type == "product" %}
- **Frontend**: React 19 with Vite
- **Styling**: Tailwind CSS + Shadcn/ui
- **Backend**: FastAPI (Python 3.11+)
- **Database**: SQLite or PostgreSQL
- **Package Manager**: bun (frontend), uv (backend)
- **LLM/AI**: Claude via Anthropic API (if applicable)
{% else %}
- **Language**: Python 3.11+
- **Package Manager**: uv or pip
- **Testing**: pytest
{% endif %}
{% if tech_stack %}
- **Additional Tech**: {{ tech_stack }}
{% endif %}

---

## Environment Setup

### Prerequisites
{% if artifact_type == "product" %}
- Node.js 20+
- Python 3.11+
- bun installed globally (for frontend)
{% else %}
- Python 3.11+
{% endif %}

### Configuration
{% if artifact_type == "product" %}
- Frontend dev server: port 3000
- Backend server: port 8000
{% endif %}

Environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
{% if artifact_type in ["agent", "product"] %}
| `ANTHROPIC_API_KEY` | Yes | Claude API key for LLM functionality |
{% endif %}
| `DEBUG` | No | Enable debug logging (default: false) |

---

## Architecture

{% if artifact_type == "tool" %}
```
┌─────────────────────────────────┐
│         CLI Tool                 │
│   ┌──────────┐  ┌──────────┐   │
│   │  Parser  │  │ Commands │   │
│   └──────────┘  └──────────┘   │
└──────────────┬──────────────────┘
               │
┌──────────────┴──────────────────┐
│         Business Logic           │
│   ┌──────────┐  ┌──────────┐   │
│   │ Services │  │ Utilities│   │
│   └──────────┘  └──────────┘   │
└─────────────────────────────────┘
```
{% elif artifact_type == "agent" %}
```
┌─────────────────────────────────┐
│         Agent Core               │
│   ┌──────────┐  ┌──────────┐   │
│   │ Planner  │  │ Executor │   │
│   └──────────┘  └──────────┘   │
└──────────────┬──────────────────┘
               │
┌──────────────┴──────────────────┐
│         LLM Layer (Claude)       │
│   ┌──────────┐  ┌──────────┐   │
│   │  Prompts │  │  Tools   │   │
│   └──────────┘  └──────────┘   │
└──────────────┬──────────────────┘
               │
┌──────────────┴──────────────────┐
│         Data Storage             │
│   ┌──────────┐  ┌──────────┐   │
│   │ Memory   │  │  State   │   │
│   └──────────┘  └──────────┘   │
└─────────────────────────────────┘
```
{% elif artifact_type == "product" %}
```
┌─────────────────────────────────┐
│         Frontend (React)         │
│   ┌──────────┐  ┌──────────┐   │
│   │  Pages   │  │Components│   │
│   └──────────┘  └──────────┘   │
└──────────────┬──────────────────┘
               │ REST API
┌──────────────┴──────────────────┐
│         Backend (FastAPI)        │
│   ┌──────────┐  ┌──────────┐   │
│   │ Routes   │  │ Services │   │
│   └──────────┘  └──────────┘   │
└──────────────┬──────────────────┘
               │
┌──────────────┴──────────────────┐
│         Database (SQLite)        │
└─────────────────────────────────┘
```
{% else %}
```
┌─────────────────────────────────┐
│         Application              │
│   ┌──────────┐  ┌──────────┐   │
│   │ Interface│  │ Business │   │
│   └──────────┘  └──────────┘   │
└─────────────────────────────────┘
```
{% endif %}

---

## Core Features

{% if artifact_type == "tool" %}
### Feature 1: CLI Argument Parsing
**Description**: Set up command-line argument parser for user inputs

**Requirements**:
- Use argparse to define CLI commands and options
- Support --help flag for usage instructions
- Validate required arguments
- Provide clear error messages for invalid inputs

**Test Steps**:
1. Run with --help flag → displays usage information
2. Run with missing required args → shows error message
3. Run with valid args → processes successfully

---

### Feature 2: Core Command Implementation
**Description**: Implement main functionality of the tool

**Requirements**:
- Process input data according to tool purpose
- Handle errors gracefully with user-friendly messages
- Output results in clear, readable format
- Support common CLI patterns (stdin/stdout)

**Test Steps**:
1. Execute core command with valid input → produces expected output
2. Execute with invalid input → shows helpful error message
3. Verify output format is readable and correct

---

### Feature 3: File I/O Operations
**Description**: Handle reading from and writing to files

**Requirements**:
- Support reading from files or stdin
- Support writing to files or stdout
- Handle file not found errors
- Validate file formats where applicable

**Test Steps**:
1. Read from input file → processes correctly
2. Write to output file → creates file with correct content
3. Handle missing input file → shows clear error

---

### Feature 4: Logging and Debugging
**Description**: Add logging support for troubleshooting

**Requirements**:
- Use Python logging module
- Support --verbose flag for detailed output
- Log important operations and errors
- Default to quiet operation

**Test Steps**:
1. Run normally → minimal output
2. Run with --verbose → shows detailed logs
3. Errors are logged with context

---

### Feature 5: Testing Suite
**Description**: Comprehensive pytest test coverage

**Requirements**:
- Unit tests for core functions
- Integration tests for CLI commands
- Test edge cases and error conditions
- Achieve >80% code coverage

**Test Steps**:
1. Run pytest → all tests pass
2. Tests cover main functionality
3. Tests verify error handling

{% elif artifact_type == "agent" %}
### Feature 1: Agent Core Setup
**Description**: Set up the agent framework and LLM integration

**Requirements**:
- Initialize Claude API client with proper error handling
- Create agent core class with planning and execution methods
- Handle API errors gracefully (rate limits, network issues)
- Support configurable model selection

**Test Steps**:
1. Initialize agent → connects to Claude API successfully
2. Test API error handling → fails gracefully with clear messages
3. Verify model configuration → uses specified model

---

### Feature 2: Prompt Management
**Description**: Create and manage prompts for the agent

**Requirements**:
- Design system prompt defining agent behavior
- Create prompt templates for common tasks
- Support dynamic prompt generation based on context
- Include few-shot examples where helpful

**Test Steps**:
1. Load system prompt → agent understands its role
2. Generate task-specific prompts → include relevant context
3. Verify prompts are well-formatted

---

### Feature 3: Tool Implementation
**Description**: Implement tools the agent can use

**Requirements**:
- Define tool schemas for Claude function calling
- Implement tool execution functions
- Handle tool errors and edge cases
- Support tool chaining where needed

**Test Steps**:
1. Agent uses tool correctly → executes and returns results
2. Tool error handling → agent recovers gracefully
3. Multiple tools work together → chain executes successfully

---

### Feature 4: Agent Execution Loop
**Description**: Main agent loop for task processing

**Requirements**:
- Accept user tasks via CLI or function calls
- Plan task execution using Claude
- Execute planned steps with tools
- Return final results to user

**Test Steps**:
1. Submit task → agent plans and executes
2. Multi-step task → agent completes all steps
3. Task fails → agent reports error clearly

---

### Feature 5: State and Memory Management
**Description**: Track agent state and conversation history

**Requirements**:
- Store conversation history for context
- Maintain state between agent steps
- Support memory persistence (optional)
- Clear state when starting new tasks

**Test Steps**:
1. Multi-turn conversation → maintains context
2. State persists across steps → agent remembers previous actions
3. Reset clears state → fresh start

{% elif artifact_type == "product" %}
### Feature 1: Project Foundation
**Description**: Set up frontend and backend scaffolding

**Requirements**:
- Initialize React app with Vite and Tailwind CSS
- Set up FastAPI backend with CORS
- Create basic folder structure
- Configure development environment

**Test Steps**:
1. Run frontend dev server → loads without errors
2. Run backend server → responds to health check
3. Frontend calls backend → CORS works correctly

---

### Feature 2: Database Setup
**Description**: Set up database schema and models

**Requirements**:
- Define database schema (SQLite or PostgreSQL)
- Create Pydantic models for data validation
- Implement database connection and migrations
- Add seed data for development

**Test Steps**:
1. Database initializes → tables created
2. Models validate data → rejects invalid input
3. Seed data loads → can query test records

---

### Feature 3: API Endpoints
**Description**: Implement REST API endpoints for core functionality

**Requirements**:
- Create CRUD endpoints for main entities
- Add request validation with Pydantic
- Implement error handling
- Document endpoints with OpenAPI

**Test Steps**:
1. Call each endpoint → returns expected response
2. Invalid requests → return 400 with error details
3. Access /docs → Swagger UI displays all endpoints

---

### Feature 4: Frontend UI Components
**Description**: Build React components for main UI

**Requirements**:
- Create reusable UI components with Tailwind
- Implement form handling and validation
- Add loading and error states
- Follow responsive design principles

**Test Steps**:
1. Components render → display correctly on desktop and mobile
2. Forms validate → show errors for invalid input
3. Loading states → display during async operations

---

### Feature 5: Frontend-Backend Integration
**Description**: Connect frontend to backend API

**Requirements**:
- Implement API client using fetch or axios
- Handle loading, success, and error states
- Display data from backend in UI
- Update UI optimistically when possible

**Test Steps**:
1. Fetch data → displays in UI correctly
2. Create/update records → reflects in UI immediately
3. Network errors → show user-friendly messages

{% else %}
### Feature 1: Core Functionality
**Description**: Implement the main functionality of the application

**Requirements**:
- Define clear interfaces and APIs
- Handle inputs and outputs correctly
- Validate data and handle errors
- Follow best practices for the chosen technology

**Test Steps**:
1. Execute main functionality → works as expected
2. Handle invalid inputs → shows errors gracefully
3. Output is correct and well-formatted

---

### Feature 2: Error Handling
**Description**: Robust error handling throughout the application

**Requirements**:
- Catch and handle exceptions appropriately
- Provide user-friendly error messages
- Log errors for debugging
- Fail gracefully without data loss

**Test Steps**:
1. Trigger error conditions → handled gracefully
2. Error messages are clear and actionable
3. Application recovers from errors

---

### Feature 3: Testing
**Description**: Comprehensive test coverage

**Requirements**:
- Unit tests for core logic
- Integration tests for workflows
- Test edge cases and error conditions
- Achieve good code coverage

**Test Steps**:
1. Run test suite → all tests pass
2. Tests cover main functionality
3. Tests verify error handling
{% endif %}

---

## Data Models

{% if artifact_type == "tool" %}
[Define data structures used by the tool, if applicable]

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class ToolConfig:
    """Configuration for the tool"""
    input_file: str
    output_file: Optional[str] = None
    verbose: bool = False
```
{% elif artifact_type == "agent" %}
```python
from pydantic import BaseModel
from typing import Literal, Optional
from datetime import datetime

class AgentTask(BaseModel):
    id: str
    description: str
    status: Literal["pending", "in_progress", "completed", "failed"]
    created_at: datetime
    completed_at: Optional[datetime] = None

class ToolCall(BaseModel):
    tool_name: str
    arguments: dict
    result: Optional[str] = None

class AgentState(BaseModel):
    current_task: Optional[AgentTask] = None
    conversation_history: list[dict]
    tool_calls: list[ToolCall]
```
{% elif artifact_type == "product" %}
```python
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class User(BaseModel):
    id: str
    name: str
    email: str
    created_at: datetime

class Item(BaseModel):
    id: str
    user_id: str
    title: str
    description: Optional[str] = None
    status: str
    created_at: datetime
```

### Database Schema
```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```
{% else %}
[Define data models appropriate for the application]
{% endif %}

---

{% if artifact_type == "product" %}
## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/health | Health check endpoint |
| GET | /api/items | List all items |
| POST | /api/items | Create new item |
| GET | /api/items/:id | Get item by ID |
| PUT | /api/items/:id | Update item |
| DELETE | /api/items/:id | Delete item |

---
{% endif %}

## File Structure

{% if artifact_type == "tool" %}
```
{{ title | lower | replace(" ", "-") }}/
├── src/
│   ├── __init__.py
│   ├── cli.py              # CLI argument parsing
│   ├── core.py             # Core functionality
│   └── utils.py            # Utility functions
├── tests/
│   ├── __init__.py
│   ├── test_cli.py
│   └── test_core.py
├── requirements.txt
├── README.md
└── init.sh                 # Startup script
```
{% elif artifact_type == "agent" %}
```
{{ title | lower | replace(" ", "-") }}/
├── agent/
│   ├── __init__.py
│   ├── core.py             # Agent core logic
│   ├── prompts.py          # Prompt templates
│   ├── tools.py            # Tool implementations
│   └── models.py           # Data models
├── tests/
│   ├── __init__.py
│   ├── test_agent.py
│   └── test_tools.py
├── .env.example
├── requirements.txt
├── README.md
└── init.sh                 # Startup script
```
{% elif artifact_type == "product" %}
```
{{ title | lower | replace(" ", "-") }}/
├── frontend/
│   ├── src/
│   │   ├── components/     # React components
│   │   ├── pages/          # Page components
│   │   ├── lib/            # API client and utilities
│   │   ├── App.tsx
│   │   └── main.tsx
│   ├── index.html
│   ├── package.json
│   └── vite.config.ts
├── backend/
│   ├── main.py             # FastAPI entry point
│   ├── routes.py           # API routes
│   ├── models.py           # Pydantic models
│   ├── database.py         # Database connection
│   └── requirements.txt
├── .env
├── README.md
└── init.sh                 # Startup script
```
{% else %}
```
{{ title | lower | replace(" ", "-") }}/
├── src/
│   ├── __init__.py
│   └── main.py
├── tests/
│   └── test_main.py
├── requirements.txt
├── README.md
└── init.sh
```
{% endif %}

---

{% if artifact_type == "product" %}
## UI/UX Design

### Design System
- **Theme**: Light with dark mode support
- **Primary color**: #3B82F6 (blue)
- **Accent color**: #10B981 (green)
- **Background**: #FFFFFF (light) / #1F2937 (dark)
- **Text**: #111827 (light) / #F9FAFB (dark)
- **Font**: Inter, system-ui, sans-serif

### Key Screens

**Main Dashboard**
- Clean, modern layout with card-based design
- Responsive grid for desktop and mobile
- Clear navigation and action buttons
- Loading states and empty states

**Detail View**
- Full details for individual items
- Edit and delete actions
- Related data display
- Breadcrumb navigation

### Key Interactions
1. User clicks create button → Form modal appears
2. User submits form → Optimistic UI update, then API call
3. User clicks item → Navigate to detail view
4. User hovers over action → Tooltip appears

---
{% endif %}

## Success Criteria

{% if artifact_type == "tool" %}
1. CLI accepts arguments and displays help correctly
2. Core functionality processes inputs and produces correct outputs
3. File I/O operations work reliably
4. Error messages are clear and helpful
5. All tests pass with >80% coverage
6. Tool can be installed and run via init.sh
{% elif artifact_type == "agent" %}
1. Agent connects to Claude API successfully
2. Agent can accept tasks and plan execution
3. Tools are implemented and work correctly
4. Agent executes multi-step tasks successfully
5. State and memory management works
6. All tests pass with good coverage
7. Agent can be started via init.sh
{% elif artifact_type == "product" %}
1. Frontend and backend start without errors
2. All API endpoints work correctly
3. Database operations (CRUD) function properly
4. UI is responsive on desktop and mobile
5. No console errors during normal usage
6. Data persists across page refresh
7. All features match the design system
8. init.sh starts the full application
{% else %}
1. Core functionality works as specified
2. Error handling is robust
3. All tests pass
4. Application runs via init.sh
5. No critical bugs or issues
{% endif %}

---

## Constraints & Notes

- This application should be built following best practices for {{ artifact_type }} development
- Code should be well-documented with docstrings
- Follow PEP 8 style guidelines for Python code
{% if artifact_type == "product" %}
- Follow React best practices and hooks patterns
- Use Tailwind CSS utility classes for styling
{% endif %}
- Include a README.md with setup and usage instructions
- The init.sh script must set up the environment and start the application
{% if artifact_type in ["agent", "product"] %}
- Ensure API keys are loaded from environment variables, never hardcoded
{% endif %}
