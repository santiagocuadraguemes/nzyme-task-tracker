#!/usr/bin/env bash
# Set up the .env file from .env.example.
# Usage: bash scripts/setup_env.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

ENV_FILE="$PROJECT_DIR/.env"
EXAMPLE_FILE="$PROJECT_DIR/.env.example"

if [ -f "$ENV_FILE" ]; then
    echo ".env file already exists. Remove it first if you want to start fresh."
    exit 1
fi

if [ ! -f "$EXAMPLE_FILE" ]; then
    echo ".env.example not found at $EXAMPLE_FILE"
    exit 1
fi

cp "$EXAMPLE_FILE" "$ENV_FILE"
echo "Created .env from .env.example"
echo "Edit $ENV_FILE to fill in your Notion API token and database IDs."
