# Agent metadata catalog test correction

Changed only packages/agent/tests/test_eval_alarm_metadata.py; no production changes and no commit.

The stale catalog test assumed every source lacked AWS coordinates. It now checks each source's exact provided alarm metadata (including ARN and source time), parsed trigger values, and rendered fields. Only missing/blank rendered values are unknown. Hostile AWS_REGION/AWS_DEFAULT_REGION=eu-west-3 cannot appear; fresh evaluation session RUN_TIME cannot replace source time. Input catalog JSON remains unchanged. Existing zero/null/absent and production-default tests are intact.

Offline verification used /private/tmp/rca-offline-review-20260911/guard/sitecustomize.py, fake AWS credentials, EC2 metadata disabled, and direct existing venv tools. The guard was explicitly confirmed to deny external DNS before tests. No AWS/model/deployment calls occurred.

- Before: old targeted test failed on the archived maintenance source's real region.
- Focused file: 30 passed.
- Full Agent: 781 passed in 16.60s.
- Ruff src/tests: passed.
- Ruff format check on changed file: passed.
- git diff --check on changed file: passed.

Logs: before.log, focused.log, full-agent.log, ruff.log, format.log, results.json in this directory.
Production defect found: none. No other package edited.
