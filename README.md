# Metroplex

## Overview

Metroplex is the Level 5 autonomy layer for the ST Metro ecosystem. It closes all three human gates: idea triage, build orchestration, and persona patch application. A CLI-only Python application that operates autonomously against configurable thresholds and upstream data sources.

## Description

Metroplex reads upstream SQLite databases, makes autonomous decisions against configurable thresholds, and drives builds via subprocess calls to queue_runner.py. It provides a complete autonomous workflow pipeline for the ST Metro development ecosystem.

## Tech Stack

- **Python 3.12** - Core runtime
- **SQLite3** - Upstream database integration
- **Pydantic v2** - Data validation and configuration
- **Jinja2** - Template rendering
- **PyYAML** - YAML configuration support

## Setup

### Quick Start

Run the initialization script to set up your development environment:

```bash
chmod +x init.sh
./init.sh
```

This will:
- Create a Python virtual environment
- Install all dependencies
- Create the data/ directory for runtime state

### Manual Setup

If you prefer manual setup:

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p data/
```

## Usage

Metroplex provides a CLI interface for autonomous decision-making and build orchestration:

```bash
# Idea triage - analyze upstream ideas and prioritize
metroplex triage

# Build orchestration - queue and drive builds
metroplex build

# Persona patch application - apply user-specific patches
metroplex patch

# Run all gates in sequence
metroplex run-all

# Check current status
metroplex status

# Reset to clean state
metroplex reset
```

## Project Structure

```
metroplex/
├── README.md              # This file
├── init.sh               # Development environment setup
├── requirements.txt      # Python dependencies
├── .gitignore           # Git ignore patterns
├── data/                # Runtime state (git-ignored)
└── metroplex/           # Main package
    ├── __init__.py
    ├── cli.py          # CLI interface
    ├── triage.py       # Idea triage logic
    ├── build.py        # Build orchestration
    └── patch.py        # Persona patch application
```

## Configuration

Metroplex is configured via YAML and environment variables. Configuration thresholds can be adjusted to control autonomous decision-making behavior.

## License

Proprietary - ST Metro Ecosystem
