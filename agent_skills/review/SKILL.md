---
name: review
description: Use when asked to review code or prose.
---

Focus on the current change. Extend the review scope only when really needed.

Questions to ask:

- Are there implicit assumptions?
    How best can they be made explicit and be linked to code and docs that
    use them?
- Are there implicit state or state transitions?
    Can we convert them to clean state machines?
- Could a reasonable reader get confused and misunderstand? In what ways?
    How to avoid?
- Does the current change affect any other parts of
    the system not thought about? What are the effects?
- What happens if something fails? Are all the failure cases covered?
    What about other things that can fail?
    When failures happen, how far do they cascade, and how can we contain them?
- Can we extract any common sets of code/docs to a deeper module/folder with
    a simple interface/summary?
    Can we make code/docs hierarchies deeper and with smaller surface area?
- Do users of the abstractions need to understand the implementation details?
    If not, is it possible, or should we not have the abstractions and
    instead use transparent constructs?
- Is the system maintainable?
    Does changing part of the system require changing lots of other parts?
    How do we reduce coupling and enforce boundaries?
- Is anything, code, docs, etc., repeated unnecessarily?
    Can they instead be shared or referred to?
- Do we have accidental complexity?
    Can we simplify as much as possible so
    we mostly only have essential complexity?
