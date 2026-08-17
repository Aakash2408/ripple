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

# Key resolution, in order: the environment, then a mode-0600 file. The file
# exists so the key never has to be typed into a shell (an interactive `read`
# gives no prompt and looks like a hang) nor pasted into a chat transcript.
KEY_FILE="${GEMINI_API_KEY_FILE:-$HOME/.gemini_key}"

if [[ -z "${GEMINI_API_KEY:-}" && -r "$KEY_FILE" ]]; then
  # Strip any trailing newline a text editor may have appended: Gemini rejects
  # a key with stray whitespace as API_KEY_INVALID, which reads like a bad key.
  GEMINI_API_KEY="$(tr -d '\r\n' < "$KEY_FILE")"
  export GEMINI_API_KEY
  echo "key source: $KEY_FILE ($(printf '%s' "$GEMINI_API_KEY" | wc -c | tr -d ' ') chars)"

  perms="$(stat -c '%a' "$KEY_FILE")"
  if [[ "$perms" != "600" && "$perms" != "400" ]]; then
    echo "warning: $KEY_FILE is mode $perms -- run: chmod 600 $KEY_FILE" >&2
  fi
fi

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  cat <<MSG
No Gemini key found.

Get a free key at https://aistudio.google.com (no credit card), then write it to
a file this script will pick up automatically:

    umask 077 && cat > $KEY_FILE      # paste, then press Ctrl-D

Or set it in the environment instead:

    export GEMINI_API_KEY=...

Do not paste a key into a chat -- it lands in a transcript on disk.
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
echo "  export ANTHROPIC_MODEL=gemini-3.5-flash-lite"
echo
echo "  -lite is the default because the larger flash models are reasoning"
echo "  models: they spend the output budget on thinking, so Ripple's small"
echo "  max_tokens call sites (20 and 256) come back with EMPTY content on"
echo "  them while -lite returns usable text at the same budget."
echo

exec "$PY" -m litellm.proxy.proxy_cli --config "$CONFIG" --port "$PORT" --host 127.0.0.1
