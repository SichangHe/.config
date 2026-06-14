---
name: prose-revision
description: Use when asked to critique prose, revise text more succinctly while preserving meaning and style, or turn an unwanted model output into a stronger future prompt.
---

Use the source prose requirement:
"Requirements R: write prose that is clear and coherent, professional and complete yet approachable and natural, as succinct and simple as possible but not too simple. Prefer verb over noun, adjective over adverb, active tense over passive tense. Avoid fluff or self-judgements. Never lose any information, or praise or repeat any given content. Keep the original language, style, and meaning as much as possible. Make minimal changes for improvement."

Use the source critique instruction:
"Criticize my draft and list all the problems. For each problem separately, quote my original words, reason about the problem, provide suggestions, and provide suggested change."

Use the source compression instruction:
"Revise this part to make it more succinct, without loosing any information. Stay close to the original language and make minimal changes."

Use the source prompt-repair instruction:
"I do not like how you PROBLEM.
What could I have said instead so that you would never ever say that?"

Additional prompt-repair procedure:

- Infer which instruction was missing or weak.
- Write the prompt wording that would have prevented the unwanted behavior.
- Make the prompt specific, strong, and testable.
