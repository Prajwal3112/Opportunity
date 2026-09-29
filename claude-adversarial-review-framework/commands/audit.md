# /audit

Run a full adversarial production-readiness assessment.

## Rules

1. Do not modify production code.
2. Begin with repository reconnaissance.
3. Build a system hypothesis.
4. Select specialist agents based on observed risk.
5. Give every agent a focused question and stop condition.
6. Prefer targeted investigation over exhaustive reading.
7. Cross-check CRITICAL/HIGH findings.
8. Use hostile review before finalizing.
9. Update PROJECT_ARCHITECTURE.md and ARCHITECTURE_AUDIT.md.
10. Record important architectural decisions as ADRs.
11. Explicitly list unknowns.
12. Do not produce an overall score.

## Deliverables

- PROJECT_ARCHITECTURE.md
- ARCHITECTURE_AUDIT.md
- relevant ADRs
- concise executive findings in the final response

## Final response

Report:
- what was established
- critical production blockers
- highest-impact findings
- important unknowns
- recommended next investigation/fix sequence

Do not dump every agent's report into the final response.
