#!/usr/bin/env bash
# Run this whenever you add or remove MP3s from sounds/win/ or sounds/lose/.
# Then reload the extension in Chrome (chrome://extensions → Reload).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

to_json_array() {
  local prefix="$1" dir="$2"
  local files=("$dir"/*.mp3)
  printf '[\n'
  local first=1
  for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    local name; name="$(basename "$f")"
    [[ $first -eq 0 ]] && printf ',\n'
    printf '    "%s/%s"' "$prefix" "$name"
    first=0
  done
  printf '\n  ]'
}

cat > "$DIR/index.json" <<EOF
{
  "win": $(to_json_array "sounds/win" "$DIR/win"),
  "lose": $(to_json_array "sounds/lose" "$DIR/lose")
}
EOF

echo "sounds/index.json updated."
echo "Win:  $(jq '.win | length' "$DIR/index.json") files"
echo "Lose: $(jq '.lose | length' "$DIR/index.json") files"
