# Awaiting reviewed model results

There are no normalized model-result fixtures for the new realistic catalog.
Do not generate them from expectations or use fake-engine output as model evidence.
The previous snapshots are in `../historical/original-four/results/`.
The default offline evaluation must fail until both engines have actual reviewed
results and a separately authorized baseline approval. This migration does not
modify `tests/baseline/`.
