#!/usr/bin/env bash
# Factor research lab end-to-end smoke.
# Exercises: CLI list → factor registration → store init → report rendering.
# Does NOT hit OKX REST (offline-safe; meant for CI + new-machine sanity).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

TMPDIR="$(mktemp -d)"
DB="$TMPDIR/zoo.db"
YML="$TMPDIR/factor_portfolio.yaml"

echo "[1/4] list — should print 15 factors"
python -m okx_trade.research list --db "$DB" --yaml "$YML" | tee "$TMPDIR/list.txt"
test "$(wc -l < "$TMPDIR/list.txt")" -ge 15

echo "[2/4] approve momentum_7d (--force, no grade yet)"
python -m okx_trade.research approve --factor momentum_7d --weight 0.3 \
       --force --db "$DB" --yaml "$YML"

echo "[3/4] verify yaml has momentum_7d entry"
grep -q "momentum_7d" "$YML"

echo "[4/4] reject momentum_7d"
python -m okx_trade.research reject --factor momentum_7d --db "$DB" --yaml "$YML"
# After reject the yaml should still parse and not contain momentum_7d
python -c "import yaml; cfg = yaml.safe_load(open('$YML')); \
    assert all(pair[0] != 'momentum_7d' for pair in cfg.get('factor_weights', []))"

rm -rf "$TMPDIR"
echo "factor_research_smoke OK"
