# Finalization: Public.com Market-Data Timestamp Investigation

## Files Created

1. **`results/investigations/multi_batch_stability_20261020_investigation.md`** (8,071 bytes)
   - Full investigation of the October 20, 2026 run: 2 network timeouts, 49 stale Last observations, 40 missing observations
   - Oct 19 vs Oct 20 comparison: FDX 36→1, BLK 31→3, AAPL 0 in both runs
   - Code review findings: no defects conclusively identified
   - Stale count correction: CDNS+PNC+HON+BLK+DE = 36 (was 39 in earlier draft)

2. **`results/investigations/public_api_support_evidence_20261020.md`** (8,093 bytes)
   - Corrected evidence table with stale counts per all 100 symbols
   - Arithmetic verification: 36/49 = 73.5% (was 79.6%)
   - Full stale/ Bid/ Ask counts per symbol
   - Oct 19/20 comparison data

3. **`results/investigations/publiccom_support_inquiry_draft.md`** (3,883 bytes)
   - Professional support inquiry asking Public.com API about timestamp semantics
   - 6 questions about Last/Bid/Ask representation, freshness, session behaviors
   - Neutral framing without alleging provider defect
   - Rationale: observed data patterns that cannot be explained from investigation data alone

4. **`results/investigations/next_test_recommendation.md`** (2,461 bytes)
   - Minimal follow-up experiment design if provider clarification is insufficient
   - 6 symbols to monitor (CDNS, PNC, HON, BLK, DE, AAPL, NVDA)
   - 5 trading sessions recommended
   - Observations to record and competing explanation patterns

5. **`results/investigations/publiccom_documentation_review_20261020.md`** (4,102 bytes)
   - Reports: Official Public.com API documentation at https://public.com/api/docs **could not be accessed**
   - Documentation review status: All 8 questions marked "NOT RETRIEVABLE — official docs not accessed"
   - Investigation-derived findings (no documentation access)
   - Conclusion: Provider support inquiry recommended

6. **`results/investigations/publiccom_support_inquiry_draft.md`** (3,883 bytes)
   - Updated support inquiry with 6 questions
   - Questions removed if answered by documentation (none were, since docs not accessed)
   - Only remaining questions retained that require provider clarification
   - References investigation data neutrally, without alleging defect

## Key Verified Figures

| Figure | Value | Notes |
|--------|-------|-------|
| CDNS + PNC + HON + BLK + DE stale observations | **36** | Correction from earlier 39 |
| 36/49 stale observations | **73.5%** | Of total 49 stale |
| 49 stale out of 17,460 recorded | **0.28%** | Stale rate Oct 20 |
| 17,411 fresh observations | **99.72%** | Fresh rate Oct 20 |
| All 100 symbols | **0 stale Bid**, **0 stale Ask** | Bid/Ask always fresh |
| Oct 19 FDX stale | **36 of 180** (20.0%) | Down to 1/175 in Oct 20 |
| Oct 19 BLK stale | **31 of 180** (17.2%) | Down to 3/175 in Oct 20 |
| Oct 19 AAPL stale | **0 of 180** (0.0%) | Consistent in Oct 20 |

## Questions in Support Inquiry (6 total)

1. What event does each Last price timestamp represent? (most recent trade, quote generation, or other)
2. Why are specific symbols' Last timestamps prone to stagnation while Bid/Ask remain fresh?
3. If no new trade occurs, is unchanged Last timestamp expected while Bid/Ask advance?
4. Are Last/Bid/Ask timestamps generated through different mechanisms?
5. Are there documented freshness guarantees, latency expectations, or session-specific behaviors?
6. What does the volume field represent and how does it relate to Last timestamps?

## No Authorized Submission Mechanism Verified

- No email address, support URL, or verified submission path was found in the project
- The support inquiry draft is ready for the user to submit through Public.com's verified channel
- No external communication was sent without explicit user approval

## No Live API Calls, No Code Changes

- All analysis performed from existing raw JSONL and summary JSON files
- No production code modifications
- No new API calls made
- Investigation data preserved intact

## Paths to the Created Reports

- `results/investigations/multi_batch_stability_20261020_investigation.md`
- `results/investigations/public_api_support_evidence_20261020.md`
- `results/investigations/publiccom_support_inquiry_draft.md`
- `results/investigations/next_test_recommendation.md`
- `results/investigations/publiccom_documentation_review_20261020.md`

## Unresolved Evidence Limitations

- Root cause of 5 symbols (CDNS, PNC, HON, BLK, DE) having stale Last timestamps while 95 others have 0
- Whether stale Last timestamps represent no new trade, delayed data, caching, or another provider-side behavior
- Whether the 60-second stale threshold is appropriately calibrated
- The dramatic FDX/BLK reduction between Oct 19 and Oct 20 (36→1, 31→3) — correlation observed but causation not established
- No access to official Public.com API documentation to determine timestamp semantics from documented contracts

## Final Statement

The investigation thoroughly analyzed the October 2026 multi-batch stability test data, correcting earlier arithmetic errors and establishing baseline figures for stale Last timestamp behavior. The observed data raises important questions about Public.com's Last/Bid/Ask timestamp semantics that cannot be answered from the investigation data alone. Provider support inquiry is the recommended next step to clarify documented API behavior.
