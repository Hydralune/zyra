# Run invalidation record

- Run: `postfix-scikit-11310-cal1`
- Classification: `invalidated_development`
- Efficacy eligible: no
- Reason: the operator interrupted the blocking API request after incorrectly treating a Docker Desktop failure as a failure of the benchmark Docker backend. The experiment actually uses the independent Docker Engine inside Ubuntu through `scripts/docker_wsl_bridge.py`.
- Evidence before interruption: the provider control-plane ledger recorded 20 successful DeepSeek dispatches, and the benchmark container contained a three-line production change in `sklearn/model_selection/_search.py`.
- Consequence: the run is permanently excluded from success-rate, cost, latency, and calibration claims. Its partial patch must not be reused as a formal sample.
- Corrective action: the harness now exposes live provider-dispatch progress while the API run request is in flight, so a quiet top-level event stream is no longer interpreted as a stalled worker.
