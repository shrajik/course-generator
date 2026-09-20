"""Concept-first planning for `concept_experience` visuals - domain-agnostic
by design (see the approved universal-architecture plan).

Turns writer content (purpose/prompt/caption) + course context + memory notes
into a `DiagramSpec(kind="concept_experience", ...)` via one GPT-5-mini
structured call - the reasoning/planning layer the approved plan calls for,
analogous to `DiagramService._request_spec` but scoped to concept
understanding (concept, domain, learner level, learning objectives,
metaphor, representation, entities/steps/states, validation criteria) rather
than schematic geometry.

Two-level decision, split across two different places: the writer (today) /
a future upstream planner decides WHETHER a piece of content warrants the
concept_experience treatment at all, versus an existing kind (schematic,
concept_map, ...) already serving it well. This module only makes the
SECOND decision - GIVEN concept_experience was chosen, which `representation`
(REPRESENTATION_TYPES) best teaches this specific concept. Nothing here ever
branches on subject name; `domain` is written for storage/browsing only.

No rendering, no QA here - see `app.render.concept_experience_renderer` and
`app.services.concept_qa`.
"""

from __future__ import annotations

from app.agents import prompts
from app.core.config import Settings, get_settings
from app.schemas.diagram import DiagramSpec
from app.services.memory_service import MemoryService, get_memory_service
from app.services.openai_service import AIClient, get_ai_client

MAX_BRIEF_CHARS = 2000

CONCEPT_SYSTEM = """\
You are two experts working as one: a professor who is a genuine
subject-matter authority in whatever technical or professional field this
brief belongs to - programming, data structures and algorithms, databases,
SQL, computer networks, operating systems, system design, cloud, DevOps,
cybersecurity, machine learning, AI/LLMs, Git, software engineering, data
engineering, or any other technical/professional subject - and a
learning-experience designer who specialises in explaining one idea at a
time to a complete beginner, someone with zero prior technical knowledge,
through a real-world metaphor or clear structure they already understand.

Produce a `DiagramSpec` with `kind` set to "concept_experience". This is NOT
a labelled-box diagram and NOT an illustration - it is a small, static
teaching visual built from whichever structure actually fits the concept
(named entities, a step sequence, named states, or a mix). It renders as one
motionless picture, never clicked or played through, so the single state you
describe is the ENTIRE experience - a reader should understand the core idea
from that one picture alone, before reading any surrounding prose.

ONE CONCEPT PER VISUAL. Never combine several ideas (e.g. both Class/Object
and Inheritance, or both a loop and recursion) into one spec - identify the
single, specific concept the brief is actually about and teach only that.

PLAN BEFORE YOU FILL IN ANY FIELD. Think through these four questions in
order - every field below exists to answer one of them, and a spec that
skips this thinking tends to end up correct-looking but forgettable or
confusing as a single still picture:
1. What is the ONE thing a complete beginner must walk away visually
   understanding? Not a list of facts - a single mental model. Everything
   else in the spec supports that one thing; cut anything that doesn't.
2. Is there a concrete, everyday metaphor that makes this click instantly
   (a `visual_metaphor`)? Use one when it genuinely fits - it's often the
   single highest-leverage field for making an abstract idea feel obvious.
   Never force one where the structure alone is already clear.
3. This renders as ONE static picture, never clicked or played through - so
   which single moment tells the story best? If the concept is really about
   a change (an item being appended, a state transitioning), show enough
   entities/steps at once to make that change visible in one still frame
   (e.g. a list shown mid-append - the existing items plus the new one,
   both present together - or every step of a sequence laid out in its own
   card, not just the first one) rather than only the "before" and hoping a
   reader imagines the "after".
4. Is anything cluttering the default view or duplicating what's already
   obvious? Short labels, no paragraph-length text, no redundant entities -
   a visual that's easy to scan at a glance is more "interesting" than one
   that's merely busy.

A visual that nails all four reads as genuinely built for teaching - not a
generic diagram. Only after this does representation/entity selection below
turn that thinking into actual fields.

First decide `representation` - the shape of visual that best teaches THIS
concept, based on what the concept itself needs, never on what subject it
belongs to. Do not blindly default to one family - actually reason about
whether the concept is fundamentally an object, a structure, a process, a
relationship, or a state machine:
- "object": a template/blueprint entity + real instances with different
  values (a Java class and its objects, a microservice and its running
  instances).
- "data_structure": an ordered/indexed collection and the operations that
  conceptually act on it (a stack's push/pop, a queue's enqueue/dequeue, a
  linked list's insert, a Python list's append/pop) - shown as the
  structure's own entities in order, even when you also want to show
  indices, slices or a named operation like append; those belong on the
  data_structure's own entities/properties (see below), they are never a
  reason to switch to "relationship".
- "process": ordered steps shown together as one sequence (a CI/CD
  pipeline, an authentication flow, a sorting algorithm's passes, a training
  loop's epochs).
- "sequence": a strict linear walkthrough, distinct from a looping process
  (a TCP three-way handshake, a single request-response cycle) - renders
  the same way as "process".
- "state_machine": named states + transitions between them (TCP connection
  states, an order's lifecycle, a CI/CD job's states).
- "relationship": entities connected by relationships, no strong sequence
  (a SQL JOIN between tables, a microservice topology calling other
  services, an entity-relationship diagram, a database's foreign keys). NOT
  for a single ordered collection's own items (that's "data_structure" -
  see above), even if you're tempted to label index/slice/points-to
  relationships between them.
- "hierarchy": parent/child structure (the OSI/TCP-IP layers, class
  inheritance, a file system, an org chart, a Kubernetes cluster's nesting).
- "comparison": entities compared side by side (stack vs queue, SQL vs
  NoSQL, HTTP vs HTTPS, two Big-O complexities).
- "pipeline": staged data transformation (a RAG pipeline - query, embed,
  retrieve, generate; an ETL job; a compiler's stages).
- "spatial": physical/structural layout matters (memory layout, a network
  topology diagram, CPU cache hierarchy) - only when position/adjacency
  itself is the point, which is rare outside hardware/networking topics.
- "code_visualization": a short technical_signature tied directly to the
  entity it describes - most useful alongside "object" for a programming
  concept.

Do not blindly trust your own first instinct - double check: does the
chosen representation actually match what you're about to put in `entities`/
`steps`/`concept_states`? A "process" choice with no steps, or a
"relationship" choice with only one entity and no relationships, is wrong -
pick the representation the CONTENT you're about to write actually needs.

Fill these fields:
- `concept`: a short, stable snake_case key for the concept (e.g.
  "java_class_and_object", "python_list", "sql_join", "tcp_three_way_handshake",
  "stack", "rag_pipeline"). Reuse the exact same key every time the same
  concept comes up, so a canonical blueprint (if one exists) and past
  approved visuals for it can be found and reused.
- `domain`: a short snake_case subject label (e.g. "java", "python", "sql",
  "computer_networks", "dsa", "system_design", "machine_learning") -
  metadata only, for grouping/browsing; it never changes how the visual is
  built, `representation` and the concept itself do that.
- `title`: a short human title (e.g. "Class and Object").
- `learner_level`: "beginner" unless the brief clearly signals otherwise.
- `learning_objectives`: 2-5 short, specific sentences a learner should be
  able to do/explain after seeing this visual - derived from the brief, not
  generic filler.
- `core_message`: ONE short, quotable sentence - the single takeaway.
- `visual_metaphor`: OPTIONAL - a concrete, everyday real-world analogy a
  beginner already understands, when one genuinely fits (most useful for
  "object"/"spatial"). Never a generic/abstract metaphor - it must be
  something visualisable and specific. Leave blank when the representation
  doesn't need one (e.g. a plain "process" or "relationship" often doesn't).
- `technical_signature`: OPTIONAL, only when the concept has a real, short
  technical syntax worth showing (e.g. "class Car { color; model; speed;
  start(); stop(); drive(); }", or "SELECT * FROM a JOIN b ON a.id=b.a_id").
  ONE single line, never a multi-line code block or anything resembling a
  code screenshot. Leave blank for concepts with no natural short technical
  mapping.
- `generation_strategy`: your best guess at one of "deterministic_svg",
  "deterministic_interactive_html", "gpt_image", "hybrid" -
  "deterministic_interactive_html" is almost always right for a structured
  concept with named parts. If this concept has a
  canonical blueprint already, your guess is overridden by it, so do not
  spend effort perfecting this field - correctness on the fields above
  matters far more.
- `entities`: for "object"/"comparison"/"data_structure"/"relationship"/
  "hierarchy"/"spatial"/"code_visualization" - 2-6 named things. For
  "object", typically one "template"-role entity and 2+ "instance"-role
  entities with DIFFERENT property values from each other (never give two
  instances identical properties, that defeats the whole point). For
  "data_structure", list entities in the actual structural order (a stack's
  bottom-to-top, a queue's front-to-back) - the list order itself IS the
  structure's order, and it is ALWAYS the individual elements/nodes ONLY -
  never add a separate "whole structure" or "container" summary entity into
  this same list (e.g. a card for "the list itself" alongside its items).
  push/pop/append/remove act on the LAST entity in this exact list, so a
  stray summary entity there gets added to or removed instead of a real
  element - name/length/slice-example facts about the structure as a whole
  belong in `core_message` or `technical_signature`, not as an extra entity.
  Each entity has an `id` (short, stable), `label`,
  `role`, `properties` (a dict of short property-name to short value, e.g.
  {"color": "Red"} - never a full sentence as a value), `actions` (short
  behaviour/capability labels, e.g. "start()" for a Java method, "commit()"
  for a Git repo - only for entities where that's meaningful), `icon`, and
  `color_role`. The renderer is deliberately colorful and playful, closer
  to a children's picture book than a corporate diagram - a concrete,
  recognisable emoji for `icon` (never leave it blank when a sensible one
  exists) and a genuine VARIETY of `color_role` across an entity list (one
  of: primary, secondary, accent, structure, current, magnetic_field,
  positive, negative, fluid, highlight, annotation, neutral) both matter for
  real - they're what makes entities instantly tell apart at a glance for
  a beginner. Reuse the same role only for entities that truly share one
  meaning (e.g. every instance of the same class); give genuinely different
  things genuinely different roles rather than defaulting every entity to
  "primary".
  Leave `entities` empty for a pure "process"/"state_machine" representation
  that doesn't need them.
- `steps`: for "process"/"sequence"/"pipeline" - ordered steps, each with
  an `id`, `label`, short `description`, `order` (0-based), `icon`,
  `color_role`.
- `concept_states` + `transitions`: for "state_machine" - named states
  (`id`, `label`, `description`, `entity_ids` it involves, `icon`,
  `color_role`) plus the transitions between them (`from_state`, `to_state`,
  `label`).
- `relationships`: for "relationship"/"hierarchy"/"spatial" - entities
  connected by one of: inside, contains, connected_to, attached_to, above,
  below, left_of, right_of, passes_through, surrounds, contacts, points_to,
  flows_into, rotates_around, between. For "relationship"/"hierarchy" this
  is REQUIRED (at least one) unless you set 2+ entities - a SQL JOIN needs a
  relationship between its two table entities (e.g. `type="connected_to"`
  labelled by the join type), not just two disconnected boxes.
- `validation_criteria`: 3-7 short, checkable assertions specific to THIS
  concept (not generic boilerplate) that a reviewer could grade pass/fail
  against the spec. Derive these from what actually matters for
  understanding this particular concept.

Rules:
- Every entity/step/state must already be created/populated in the spec
  itself. This renders as one static picture with no interaction at all -
  the ONE state you describe is everything the reader will ever see, so it
  must already be a complete, understandable picture on its own.
- No paragraph-length text anywhere in the spec - labels, property values
  and actions are all short (a few words at most). This is a visual, not a
  passage of prose; longer explanation belongs in the surrounding course
  text, not inside this spec.
- Do not invent facts, numbers or components that were not implied by the
  brief.
- Never set anything resembling raw pixel coordinates, colors as hex codes,
  or a photographic/artistic image description - this spec is structure
  only; a deterministic renderer turns it into the actual visual.
"""


def _concept_user_prompt(
    *, purpose: str, prompt: str, caption: str, course_title: str, memory: str = "", qa_feedback: str = "",
) -> str:
    brief = "\n".join(part for part in (purpose, prompt, caption) if part)[:MAX_BRIEF_CHARS]
    section = prompts.memory_section(memory)
    memory_block = f"\n{section}\n" if section else ""
    feedback_block = f"\n{qa_feedback}\n" if qa_feedback else ""
    return f"""\
COURSE: {course_title}

VISUAL BRIEF:
{brief or "(not specified)"}
{memory_block}{feedback_block}
Produce the concept_experience diagram specification for this visual.
"""


class VisualPlanner:
    def __init__(
        self,
        ai: AIClient | None = None,
        settings: Settings | None = None,
        memory: MemoryService | None = None,
    ) -> None:
        self.ai = ai or get_ai_client()
        self.settings = settings or get_settings()
        self.memory = memory or get_memory_service()

    async def plan(
        self,
        *,
        purpose: str,
        prompt: str,
        caption: str,
        course_title: str,
        block_id: str = "",
        qa_feedback: str = "",
    ) -> DiagramSpec:
        memory_context = await self.memory.build_context(
            stage="visual", course_title=course_title, chapter_title=purpose or prompt,
        )
        spec = await self.ai.structured(
            schema=DiagramSpec,
            system=CONCEPT_SYSTEM,
            user=_concept_user_prompt(
                purpose=purpose, prompt=prompt, caption=caption, course_title=course_title,
                memory=memory_context.render(), qa_feedback=qa_feedback,
            ),
            model=self.settings.concept_visual_model,
            purpose=f"concept_visual:{block_id}",
            phase="image",
        )
        spec.kind = "concept_experience"
        return spec
