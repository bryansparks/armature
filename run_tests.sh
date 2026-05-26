#!/usr/bin/env bash
# Run the Armature test suite.
# Usage: ./run_tests.sh [pytest options]
# Examples:
#   ./run_tests.sh                    # all tests
#   ./run_tests.sh -k test_engine     # filter by name
#   ./run_tests.sh tests/state/       # specific directory
#   ./run_tests.sh --cov armature     # with coverage

set -e

python -m pytest tests/ \
  --tb=short \
  --color=yes \
  "$@"
