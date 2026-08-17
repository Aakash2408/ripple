#!/usr/bin/env bash
# Start the LiteLLM translation proxy so Ripple's LLM path can be exercised
# locally against a free Gemini tier.
#
# WHY THIS SCRIPT EXISTS RATHER THAN JUST `litellm --config ...`
#
# 1. The toolbox python3.12 wrapper supplies a library path that the REAL
#    interpreter binary does not have on its own. Without LD_LIBRARY_PATH set,
#    installing or running anything that introspects the interpreter (maturin,
#    used by tiktoken) fails with:
#       libpython3.12.so.1.0: cannot open shared object file
#    That is also why `python -m venv` cannot produce a working venv here.
#
# 2. tiktoken has no sdist-buildable path without a Rust toolchain, and there is
#    no cargo on this box. It must come from a prebuilt wheel, hence
#    --prefer-binary at install time.
#
# TEST USE ONLY -- see tools/litellm_config.yaml for the data-handling reason.

set -euo pipefail

TB=/home/aakkaash/.toolbox/tools/meshclaw/3.3.7
export LD_LIBRARY_PATH="$TB/python3.12/lib:$TB/lib:${LD_LIBRARY_PATH:-}"
PY="$TB/python3.12/bin/python3.12"
CONFIG="$(dirname "$0")/litellm_config.yaml"
PORT="${LITELLM_PORT:-4000}"

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  cat <<'MSG'
GEMINI_API_KEY is not set.

Get a free key at https://aistudio.google.com (no credit card), then:

    read -rs GEMINI_API_KEY && export GEMINI_API_KEY

`read -rs` keeps it off the screen and out of shell history. Do not paste a key
into a chat -- a GitHub token was revoked by secret scanning that way already.
MSG
  exit 1
fi

if ! "$PY" -c "import litellm" >/dev/null 2>&1; then
  echo "litellm is not installed. Install it with:"
  echo "  LD_LIBRARY_PATH=$TB/python3.12/lib:$TB/lib \\"
  echo "    $PY -m pip install --user --prefer-binary 'litellm[proxy]'"
  exit 1
fi

echo "starting LiteLLM on http://127.0.0.1:$PORT"
echo "  config : $CONFIG"
echo "  models : $(grep -oP '(?<=model_name: ).*' "$CONFIG" | tr '\n' ' ')"
echo
echo "then point Ripple at it, in another shell:"
echo "  export ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT"
echo "  export ANTHROPIC_AUTH_TOKEN=DUMMY      # do NOT also set ANTHROPIC_API_KEY"
echo "  export ANTHROPIC_MODEL=gemini-2.5-flash"
echo

exec "$PY" -m litellm.proxy.proxy_cli --config "$CONFIG" --port "$PORT" --host 127.0.0.1
