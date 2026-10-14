# Data and metric definitions

## Source

The demo uses the [official Synthea CSV sample](https://synthetichealth.github.io/synthea-sample-data/). The archive retrieved on October 13, 2026 has 108 distinct synthetic patients and 5,571 encounters. The publisher labels it as a 100-patient sample; the observed count is recorded separately.

[data/artifact.lock.json](../data/artifact.lock.json) records the source URL, checksum, member headers, and counts. The SHA-256 is `d61417b551e5b0997c33851b339c157421751f0ea68c18ea686ceb1850907c35`. The mutable `latest` download is accepted only when it matches that fingerprint. The repository does not redistribute the archive or claim its generating Synthea version.

Synthea reference: Walonoski et al., *Synthea: An approach, method, and software mechanism for generating synthetic patients and the synthetic electronic health care record*. JAMIA, 2018. [doi:10.1093/jamia/ocx079](https://doi.org/10.1093/jamia/ocx079).

## Preparation

Only `encounters.csv` and the payer mapping from `payers.csv` are used. Facility and provider keys, clinical codes, and other unused columns are dropped locally. Private payer names become `Private Insurance`; the other categories are Medicare, Medicaid, Dual Eligible, and No Insurance. Unknown payer names stop preparation.

Synthetic encounter, patient, and payer IDs remain in the prepared extracts and some underlying tables. They are not metric-view fields. Anyone with access to the tables or volume can still read the synthetic IDs. This is data minimization, not a de-identification process for real patients.

## Metric view

The view `encounter_operations_metrics` has five dimensions: Encounter Date, Quarter, Year, Class, and Payer Category. A many-to-one join adds the payer category. SQL gates verify unique payer keys and complete encounter matching.

| Measure | Formula | Additive across groups |
|---|---|---|
| Encounter Count | Count encounters | Yes |
| Unique Patient Count | Count distinct synthetic patient IDs | No |
| Total Claim Cost | Sum simulated claim cost | Yes |
| Total Payer Coverage | Sum simulated payer coverage | Yes |
| Claim Cost Not Covered by Payer | Claim cost minus payer coverage | Yes |
| Coverage Rate | Payer coverage divided by claim cost, as a ratio of sums | No |
| Average Claim Cost | Average simulated claim cost per encounter | No |
| Encounters per Patient | Encounter count divided by distinct patient count | No |

Recompute the non-additive measures at the requested grain. Do not sum unique patient counts or average separate group rates. Claim Cost Not Covered by Payer does not mean an amount a person paid or owes.

All dates and monetary values are simulated. Empty groups are omitted rather than filled with zero. Money and per-patient values are rounded to two decimals; Coverage Rate is compared as a fraction at four decimals. Dual Eligible has very few synthetic encounters and cannot support real-world conclusions.

See [the metric-view SQL](../sql/30_metric_view.sql) and [Agent instructions](../genie/instructions.md).

## Updating the source

If the official download changes, the normal command stops on a checksum mismatch. A maintainer can inspect the new archive and propose a new lock with `scripts/download_and_verify.py --rebaseline --accept-sha256 <new_hash>`.

Review the proposed lock and payer mapping before accepting it. Update the pinned checksum deliberately, recompute the reference digests, and rerun the local and workspace checks. Existing result counts describe the old pinned sample until a new experiment is recorded. Do not change the checksum just to bypass a failed download.
