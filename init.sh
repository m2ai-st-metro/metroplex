#!/bin/bash

set -e

echo "Initializing Metroplex development environment..."

# Use parent project's venv (has all dependencies pre-installed)
PARENT_VENV="/home/apexaipc/projects/yce-harness/venv"

if [ ! -d "$PARENT_VENV" ]; then
    echo "ERROR: Parent venv not found at $PARENT_VENV"
    exit 1
fi

echo "Using parent project's venv at $PARENT_VENV"

# Install project dependencies
if [ -f "requirements.txt" ]; then
    echo "Installing dependencies from requirements.txt..."
    uv pip install -q -r requirements.txt
fi

# Create data directory
mkdir -p data/

echo "Metroplex initialized"
echo "To run tests: $PARENT_VENV/bin/python -m pytest tests/ -v"
