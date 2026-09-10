---
state: killed
lens: telemetry
created: 2026-09-10
metric: design/roadmap/post.md present on origin/main
before: 0 (measured 2026-09-10; git cat-file -e origin/main:design/roadmap/post.md exits 128)
target: 1
measure: git cat-file -e origin/main:design/roadmap/post.md >/dev/null 2>&1 && echo 1 || echo 0
evidence:
  - design/roadmap/evidence/2026-09-10-post-md-missing-on-trunk.txt
slices: 1/1
after:
---
# Grammar-closure file for slug post

## Why, against GOAL.md

This is not a product invent. The 2026-09-10 04:34 channel message used the words that the manager grammar reads as a proposal slug `post` (`this PROPOSAL post`). Hel1 then required `design/roadmap/post.md` on the trunk. Measure printed 0 (`design/roadmap/evidence/2026-09-10-post-md-missing-on-trunk.txt`). GOAL.md numbers are unchanged. `state: killed` so this file does not occupy the proposed cap or start an epic.

## What better looks like

The file exists on origin/main with metric/before/target/measure and evidence, so check_proposal_fields for slug `post` can PASS. No dispatch, no product code.

## Slices

- [x] `design/roadmap/post.md` on origin/main; measure command prints 1.
