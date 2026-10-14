# Results from the sample experiment

The recorded October 14, 2026 experiment used one Databricks sandbox and Databricks CLI 1.20.0. Each analytical question started a new Chat conversation. The questions were repeated in three batches; each boundary question was asked once. There were 70 prompt submissions in total.

| Test | Recorded result |
|---|---|
| Seven checked SQL requests | 7/7 matched the local Python reference |
| Four additional checked SQL requests | 4/4 matched the local Python reference |
| Fourteen analytical questions | 14/14 in each batch, 42/42 overall |
| Four additional analytical questions | 4/4 in each batch, 12/12 overall |
| Ten original boundary questions | 9/10 passed the fixed rubric |
| Six boundary paraphrases | 6/6 passed the fixed rubric |

The boundary miss asked whether Medicaid coverage caused emergency encounters to change. Genie rejected the causal claim, but its answer text did not offer the aggregate alternative required by the rubric. The miss remains counted.

The historical result comparator checked values after fixed rounding and ignored column labels. The current checker also validates semantic labels and accepts legitimate column reordering. Historical scores retain their original meaning. The recorded numerical tables were also rechecked locally against the independent reference during preparation of this release.

The manual review covered the first analytical batch from each suite and all 16 boundary answers. Later analytical repeats were checked for result values, not separately documented as complete language reviews.

Benchmark questions and checked SQL are stored in the Agent's evaluation section, separate from response instructions and examples. Databricks states that [benchmarks do not improve Genie context](https://docs.databricks.com/aws/en/genie-agents/concepts). The additional questions are absent from the configuration. Both sets were selected by the author and are small; neither establishes broad generalization.

These results apply to the pinned synthetic sample, recorded configuration, and fixed questions. They do not establish product-wide accuracy, clinical suitability, or production readiness. They are not a promise of identical future answers.
