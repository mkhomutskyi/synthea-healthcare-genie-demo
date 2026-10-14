# Third-party notices

## Synthea and the Synthea sample data

- Synthea is open-source software from The MITRE Corporation, licensed under the Apache License, Version 2.0 (https://github.com/synthetichealth/synthea).
- The sample data archive is published at https://synthetichealth.github.io/synthea-sample-data/. When it was retrieved on 2026-10-13, that sample-data repository did not declare a license. This project therefore makes no license claim for the data.
- The data is not redistributed here. No archive, extracted CSV or staged extract is committed or included in any release. Each user downloads it directly from the publisher.
- Some excluded columns of the archive carry clinical terminology codes and descriptions. This project drops them locally and never uploads or publishes them. Third-party terminology licenses therefore are not exercised by the published outputs, but your own use of the full archive may be subject to them.

Please cite Synthea when you use this demo:

Walonoski J, Kramer M, Nichols J, Quina A, Moesel C, Hall D, Duffett C, Dube K, Gallagher T, McLachlan S. Synthea: An approach, method, and software mechanism for generating synthetic patients and the synthetic electronic health care record. Journal of the American Medical Informatics Association. 2018;25(3):230-238. doi:10.1093/jamia/ocx079

## Trademarks and non-endorsement

Synthea and MITRE are trademarks of their respective owners. They are used here only to identify the data source. Their use does not imply that The MITRE Corporation or the Synthea project endorses this project or is affiliated with it.

Databricks, Genie and Unity Catalog are trademarks of Databricks, Inc. They are used here to identify the platform. This is an independent demo project, not an official Databricks product.

## Python dependencies

Runtime: PyYAML (MIT). Development only: pytest (MIT), ruff (MIT), mypy (MIT), types-PyYAML (Apache-2.0). The exact versions are pinned in `uv.lock`. They are installed from PyPI and not vendored.
