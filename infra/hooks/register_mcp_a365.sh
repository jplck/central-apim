#!/bin/sh
# Post-deploy hook: register the energy MCP server with Microsoft Agent 365 (BYO MCP).
# Implements Phase 0 (evaluate) + Phase 1 registration as NoAuth (the server is
# unauthenticated) from mcp_tools_a365.md.
#
# Opt-in and non-fatal by default: it only acts when ENABLE_A365_MCP_REGISTER is set, and a
# missing `a365`/`az` CLI or a failed call just warns so `azd up` stays green. Set
# A365_STRICT=1 to make a failure abort the provision instead.
#
# Env (all optional except the gate):
#   ENABLE_A365_MCP_REGISTER  gate: unset/false=skip, dryrun=--dry-run, true/1=register
#   MCP_URI                   server URL (azd output; e.g. https://<fqdn>/mcp)
#   A365_MCP_SERVER_NAME      default ext_energymcp (must start ext_, <=20 chars)
#   A365_EVAL_ENGINE          default none (none|auto|github-copilot|claude-code)
#   A365_MCP_REGISTER_FILE    optional -f payload path; auto-detected at
#                             src/mcp-energy/a365-register.json. Also settable via -f/--input-file.
#   A365_STRICT               1 = fail the hook on error (default: warn only)

HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

SERVER_NAME="${A365_MCP_SERVER_NAME:-ext_energymcp5}"
TOOLS="get_customer_profile,list_meters,get_meter_readings,get_consumption_summary"
PUBLISHER="Contoso Energy (demo)"
DESCRIPTION="Energy customer + meter profile demo (fake data, 5 customers)"
EVAL_ENGINE="${A365_EVAL_ENGINE:-none}"
EVAL_OUT="$HOOK_DIR/.a365-eval"

warn() { echo "  ! $*" >&2; }

# Warn (and optionally abort under A365_STRICT); otherwise skip cleanly so azd stays green.
fail() {
  warn "$*"
  if [ "${A365_STRICT:-}" = "1" ]; then
    exit 1
  fi
  echo "  (non-fatal; set A365_STRICT=1 to make this abort the deploy)"
  exit 0
}

# A365 CLI rule: server name must start with ext_ and be at most 20 chars.
validate_name() {
  case "$1" in ext_*) ;; *) return 1 ;; esac
  [ "${#1}" -le 20 ] || return 2
  return 0
}

# --- args: -f/--input-file <json> supplies a full registration payload (tool descriptions
#     included, so Phase 1 never prompts); --check runs the CLI-free self-test. ---
INPUT_FILE="${A365_MCP_REGISTER_FILE:-}"
DO_CHECK=""
while [ $# -gt 0 ]; do
  case "$1" in
    -f|--input-file) INPUT_FILE="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
    --check)         DO_CHECK=1; shift ;;
    *)               warn "ignoring unknown arg: $1"; shift ;;
  esac
done

# Default to the repo's registration JSON when present, so Phase 1 is non-interactive.
DEFAULT_FILE="$HOOK_DIR/../../src/mcp-energy/a365-register.json"
[ -z "$INPUT_FILE" ] && [ -f "$DEFAULT_FILE" ] && INPUT_FILE="$DEFAULT_FILE"

# Self-check: exercises validate_name and reports the payload source. No CLIs required.
if [ "$DO_CHECK" = "1" ]; then
  validate_name "$SERVER_NAME"                  || { echo "FAIL: '$SERVER_NAME' should be valid"; exit 1; }
  validate_name "energymcp"                     && { echo "FAIL: prefix rule not enforced"; exit 1; }
  validate_name "ext_this_name_is_too_long_xx"  && { echo "FAIL: length rule not enforced"; exit 1; }
  if [ -n "$INPUT_FILE" ]; then
    [ -r "$INPUT_FILE" ] || { echo "FAIL: input file '$INPUT_FILE' not readable"; exit 1; }
    echo "self-check ok: name '$SERVER_NAME' valid; payload from file: $INPUT_FILE"
  else
    echo "self-check ok: name '$SERVER_NAME' valid; tools: $TOOLS"
  fi
  exit 0
fi

# --- gate ---
case "${ENABLE_A365_MCP_REGISTER:-}" in
  ""|false|0|no)
    echo "A365 MCP registration skipped (set ENABLE_A365_MCP_REGISTER=true, or =dryrun to preview)."
    exit 0 ;;
esac
DRYRUN=""
[ "${ENABLE_A365_MCP_REGISTER}" = "dryrun" ] && DRYRUN="--dry-run"

# --- preconditions ---
[ -n "${MCP_URI:-}" ] || fail "MCP_URI is empty (run via azd so the output is exported)."
validate_name "$SERVER_NAME" || fail "Server name '$SERVER_NAME' must start with ext_ and be <=20 chars."
command -v a365 >/dev/null 2>&1 || fail "a365 CLI not found. Install Agent 365 CLI >=1.1.165-preview (needs the ASP.NET Core 8 runtime: 'sudo apt-get install -y aspnetcore-runtime-8.0')."
if command -v az >/dev/null 2>&1; then
  az account show >/dev/null 2>&1 || fail "Not logged into az. Run 'az login' (register uses the az login tenant)."
else
  fail "az CLI not found. Agent 365 register uses the current 'az login' tenant."
fi

echo "==> Agent 365 BYO MCP: server '$SERVER_NAME' -> $MCP_URI"

# --- Phase 0: evaluate tool schemas (advisory; never fatal) ---
echo "--> Phase 0: evaluate ($EVAL_ENGINE)"
mkdir -p "$EVAL_OUT"
if a365 develop-mcp evaluate --server-url "$MCP_URI" --eval-engine "$EVAL_ENGINE" --output-dir "$EVAL_OUT"; then
  echo "    evaluation report in $EVAL_OUT"
else
  warn "evaluate failed/skipped (advisory only); continuing to registration."
fi

# --- Phase 1: register (auth from payload, or from env in the flag fallback) ---
# Prefer a full JSON payload (-f) so tool descriptions are supplied non-interactively;
# --server-url still overrides the file's URL to stay env-driven. Fall back to name flags
# (which prompt for each tool description) only when no payload file is available.
echo "--> Phase 1: register-external-mcp-server (auth from payload) $DRYRUN"
# shellcheck disable=SC2086  # $DRYRUN is an intentional optional flag, unquoted on purpose.
if [ -n "$INPUT_FILE" ]; then
  [ -r "$INPUT_FILE" ] || fail "input file '$INPUT_FILE' not readable."
  echo "    payload: $INPUT_FILE"
  a365 develop-mcp register-external-mcp-server \
      -f "$INPUT_FILE" \
      --server-url "$MCP_URI" \
      $DRYRUN
else
  # No payload file: bare NoAuth registration (the server is unauthenticated).
  # shellcheck disable=SC2086  # $DRYRUN is an intentional optional flag, unquoted on purpose.
  a365 develop-mcp register-external-mcp-server \
      --server-name "$SERVER_NAME" \
      --server-url "$MCP_URI" \
      --auth-type NoAuth \
      --publisher "$PUBLISHER" \
      --description "$DESCRIPTION" \
      --tools "$TOOLS" \
      $DRYRUN
fi
if [ $? -eq 0 ]; then
  echo "==> Registered. A tenant admin now approves it in the M365 admin center"
  echo "    (Agents > Tools > Requests); propagation to Copilot Studio can take ~30 min."
else
  fail "register-external-mcp-server failed."
fi
