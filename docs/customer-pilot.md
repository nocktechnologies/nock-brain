# Customer pilot acceptance checklist

This checklist is for a person trying a fresh customer Brain. Automated
synthetic tests are regression evidence, not a substitute for this feedback.
Do not send raw transcripts, signing keys or a copied store as a pilot report.

## First session

1. Follow [customer setup](customer-setup.md) with a fresh store outside a repo.
   Record the OS, Python version, key algorithm and time to first accepted fact.
2. Import a small selected source containing a decision, a preference and an
   instruction. Read the entire proposal. Record which useful facts were missed
   and which candidates were irrelevant, inaccurate or incorrectly attributed.
3. Apply the reviewed digest, open Explorer and locate every accepted fact.
   Check that source evidence and the verification label make sense.
4. Generate session hook settings for one selected transcript directory. Start
   Claude with the printed command. Inspect `/hooks` first: existing fleet,
   managed and plugin hooks are not removed by the customer settings file.
   Use a setup without fleet hooks when testing customer isolation.
   Make a tagged decision in that conversation,
   then inspect the pending queue. Confirm it is absent from accepted memory
   until you review and apply it.
5. Ask a direct question and a differently worded question about each accepted
   decision. Record whether recall ran and whether the returned notes helped.
   A BM25 match depends on shared terms; semantic recall is not enabled here.

## Change and removal

1. Propose a one-record correction, review it and apply. Confirm the old content
   is in Explorer history and the new fact is the current recall result.
2. Propose forgetting a record, review it and apply. Refresh Explorer and confirm
   it is gone; confirm saved proposals have been cleared.
3. Reimport the original source. Applying a reviewed proposal must skip the
   forgotten ID. The original file itself will still contain the text.
4. Start a second fresh store. Its identity, key, facts and proposals must be
   independent of the first store. Do not reuse a fleet store for this check.

## Report and release decision

Record counts for useful candidates accepted, useful facts missed, irrelevant
candidates rejected, and useful recall results out of attempted questions.
Include setup friction, unclear error messages and any correction/forgetting
surprises. Use invented examples or manually sanitized excerpts if needed.

A stable release needs repeatable successful setup by people outside the team,
acceptable extraction and recall on their own work, understandable review and
removal controls, and a supported distribution/update path. The repository's
36-query recall gate measures a committed fixture, not these customer outcomes.
No customer pass rate is claimed by the implementation or synthetic test suite.
