# /verify

Re-validate existing audit findings.

For each CRITICAL/HIGH finding:
1. locate evidence
2. inspect surrounding implementation
3. search for mitigating behavior
4. attempt to falsify the finding
5. adjust confidence/severity if necessary

This command is intentionally skeptical of the previous audit.
