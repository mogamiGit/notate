#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

chmod +x "$DIR/notate.py"
mkdir -p ~/.local/bin
ln -sf "$DIR/notate.py" ~/.local/bin/notate

echo "✅ Installed: ~/.local/bin/notate -> $DIR/notate.py"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) echo "✅ ~/.local/bin is on your PATH" ;;
  *) echo "⚠️  Add ~/.local/bin to your PATH to run 'notate' from anywhere" ;;
esac
echo "ℹ️  Create ~/.env.notate with your API keys (see .env.example)"
