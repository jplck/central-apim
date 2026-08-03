#!/bin/sh
# Post-provision hook: create the prompt agent in each consumer project, then (if the
# shared ACR was provisioned) build and deploy the tiny hosted agent into each project.
# Uses a local venv so it never touches the system Python.
set -e

HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV="$HOOK_DIR/.venv"

if [ ! -d "$VENV" ]; then
  echo "Creating hook virtual environment..."
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
. "$VENV/bin/activate"

pip install --quiet --disable-pip-version-check --upgrade "azure-ai-projects>=2.3.0" azure-identity

python "$HOOK_DIR/create_agents.py"

# Hosted (containerized) agents: build the image on ACR and register it in each
# consumer project. Only when hosted agents were provisioned (ACR_NAME output set).
if [ -n "${ACR_NAME:-}" ]; then
  python "$HOOK_DIR/create_hosted_agents.py"
fi

# Demo MCP server: build src/mcp-energy into its ACR and swap it onto the container
# app. Only when the MCP module was provisioned (MCP_APP_ID output set).
if [ -n "${MCP_APP_ID:-}" ]; then
  python "$HOOK_DIR/deploy_mcp.py"
fi
