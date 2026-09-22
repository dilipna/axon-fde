# Published runs

A run in this directory backs a number that appears somewhere a reader can
see it: `docs/evaluation/claims.md`, `docs/evaluation/results.md`, the README.

**Why these are committed when `benchmarks/results/*.json` is ignored.**
Invariant I7 says no number ships without a stored benchmark run behind it. A
run stored only on the machine that produced it satisfies the letter of that
and none of its purpose: nobody else can check the figure, and "we measured
zero approval bypasses" becomes a claim about somebody's memory. Exploratory
runs stay ignored; promoting one to back a claim is a deliberate act that
puts the file here.

A run is only eligible if its `provenance.reproducible` is `true`. A number
measured against an uncommitted tree cannot be reproduced by anyone, so it may
be recorded but never published.
