#!/usr/bin/env sh
set -eu
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
export VISUAL_QA_MANDATORY_EVIDENCE_DIR=.runtime/visual-qa-mandatory
export PF03E2_PROJECTFLOW_SOURCE_SHA="$(git rev-parse HEAD)"
export PF03E2_PROJECTFLOW_SOURCE_TREE="$(git rev-parse HEAD^{tree})"
python -m pytest -p no:cacheprovider -vv --show-capture=no \
  tests/test_visual_qa_mandatory_sandbox.py \
  --junitxml=.runtime/visual-qa-mandatory/junit.xml
