# Documentation handoff — 2026-11-01

- Review base: `0f5e6ee2cf5f5e884358635dcd835590b980a1ae`
- Evidence/documentation head: `498c41bb9d1504ccfaaf28eea4fee4478dc0a6b7`
- Reading order: README.md → RESULTS.md → GPT_CONTEXT.md

Made the later author-run optimized retrieval cohort easy to locate, while retaining the original R02 scores and negative reranking result. Added the existing source-ack meaning without changing the reported visibility timing.

Implementation, saved run artifacts and original measured values are unchanged. This update adds no new experiment, code execution or independent rerun. Author self-test summaries remain identified by their source; private raw files are not added to the public repository.

Review question: Check that the optimized self-test and original R02 are separate cohorts, and that source ack denotes the publication response rather than a message-consumer acknowledgement.

The later commit carrying this handoff is metadata only; the evidence/documentation head above stays fixed.
