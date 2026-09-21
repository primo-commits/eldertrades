#!/usr/bin/env bash
# Build the double-click distribution zip from a clean git export.
#
# Uses `git archive` rather than copying the working tree, so the package can
# only ever contain committed files -- no stray build output, caches, or a real
# alpaca_keys.txt holding live credentials.
set -euo pipefail
cd "$(dirname "$0")"

OUT="elder-trades.zip"
rm -rf dist_build "$OUT"
mkdir -p dist_build

git archive --format=tar --prefix=eldertrades/ HEAD | tar -x -C dist_build

# Ship the credential TEMPLATE under the real filename, so the user edits it
# in place. The real alpaca_keys.txt is gitignored and never packaged.
mv dist_build/eldertrades/alpaca_keys.txt.template dist_build/eldertrades/alpaca_keys.txt

find dist_build -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find dist_build -name '*.pyc' -delete 2>/dev/null || true

(cd dist_build && zip -rq "../$OUT" eldertrades)

echo "built $OUT ($(du -h "$OUT" | cut -f1))"
echo "verifying from a clean extraction..."
T=$(mktemp -d)
unzip -q "$OUT" -d "$T"
( cd "$T/eldertrades" \
  && python3 -m tests.test_pipeline   | tail -1 \
  && python3 -m tests.test_reconciler | tail -1 )
rm -rf "$T"
