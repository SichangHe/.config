---
name: avoid-sounding-like-llm
description: Rewrite agent reports, issue drafts, examples, critiques, emails, and collaborator-facing prose so they do not sound generic, bot-like, academic, evasive, or like chat-assistant slop. Use when revising text for the human, external readers, collaborators, bug reports, launch materials, or verification examples; apply issue-tree and audience-splitting rules only when the text is an issue tree or multi-audience document.
---

# Avoid Sounding Like LLM

Use this skill to make prose direct, concrete, and audience-native. Preserve the meaning, but remove generic assistant framing, bookkeeping narration, vague safety language, and inflated caution.

Do not delete human-authored quotes, inline comments, or reviewer feedback while making prose sound better. Preserve them exactly when they are evidence or requested content. If they do not belong in the outward text, move them to an internal note or say where they should go.

## Core Checks

- Lead with the answer or concrete claim.
- Write for the actual reader, not for a chat transcript.
- For issue trees, put each point in exactly one place.
- Use ordinary sentences instead of label fragments.
- Explain technical syntax by naming what each part does.
- Keep examples simple, but keep the explanation precise.
- For multi-audience documents, separate internal notes from outward-facing text.
- State uncertainty in human language instead of audit labels.

## Bad / Better Patterns

Bad: "Related reviewer comments are preserved..."
Better: Make the issue tree itself show the relationship. Do not narrate bookkeeping to the reader.

Bad: "Before the updated Common Crawl scoring can support..."
Better: State the concrete issue now. Do not justify it through future project machinery.

Bad: "Keep the current primary plan."
Better: "The primary plan is ..." or "I would keep ..."

Bad: "Treat this as the framing issue."
Better: "I think this is a framing issue."

Bad: "The revision should state..."
Better: "We should state that ..." or write the revised sentence directly.

Bad: "Stated reason for 2014."
Better: "The reason for using 2014 is ..."

Bad: "Inference, not confirmed."
Better: "I think ..., but we have not confirmed it."

Bad: Long recaps of what changed when the human asked for file names or lines.
Better: Give only the file name and line number unless the human asked for explanation.

Bad: Repeating the same rationale across several issues.
Better: In an issue tree, put the rationale once in the issue where it belongs.

Bad: Flattening issue feedback into a loose list.
Better: Preserve the major/minor/nit split when the review uses it, and keep a strict hierarchy without parent/sibling cross-references.

Bad: Writing the answer as if it is feedback to the chat user.
Better: Write it as collaborator-facing issue prose.

Bad: Safe-sounding arbitrary numbers like "target about 500 held-out sites" without scale reasoning.
Better: Separate the sample-size decision from the options and justify the scale.

Bad: "Human annotation note."
Better: Quote the reviewer point and say plainly what issue it answers.

Bad: Long technical detours before answering the question.
Better: Answer the original question in short first, then add detail.

Bad: Mixing internal product notes, collaborator-facing issue prose, and engineer-facing examples.
Better: In a multi-document or multi-section deliverable, use one audience per document or section.

Bad: Stating "this document is for software engineers" inside the document.
Better: Write in the right voice for that audience without announcing the audience.

Bad: Asking the audience whether something adds value.
Better: State the concrete value claim and support it.

Bad: Complex verification demos that hide the point.
Better: Use short examples that still show the verification value.

Bad: Simplifying examples by deleting the explanation.
Better: Keep code simple and explain the symbolic inputs, model, property, and expected result.

Bad: Repo-internal labels like "Standalone CBMC file" in shareable text.
Better: Move local paths, timings, and repo-specific labels to internal notes.

Bad: Weak generic names like "modeled race condition."
Better: Show two actual threads and the actual synchronization, or say plainly that the example is only a model and not a concurrency demonstration.

Bad: Calling bounded checking a proof.
Better: Say it found no counterexample within the declared bounded domain; name the bounds and limits.

## Revision Procedure

1. Identify the actual reader: human, collaborator, external reader, developer, VC, manager, or internal agent.
2. Remove chat-assistant commands, generic guardrail language, and process bookkeeping unless the reader needs it.
3. Rewrite label fragments as ordinary sentences.
4. Put the answer first.
5. Keep uncertainty, limits, and evidence, but say them plainly.
6. Preserve exact human-authored wording when it is quoted evidence, an inline comment, or requested content.
7. Preserve the human's intended style. If the human wants launch or manifesto prose, do not turn it into academic qualification.
