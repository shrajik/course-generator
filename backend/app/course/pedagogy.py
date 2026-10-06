"""The educational model lookup matrix, as deterministic code.

Client requirement: the backend "acts as an automated instructional designer"
- given the learner profile it must pick the instructional model, the
structural strategy, the output mechanics and the cross-domain analogy set,
and force the writing agents to obey them.

Everything here is a pure function of `LearnerProfile`: no AI call decides
which model applies. The single entry point is `learner_brief(profile)`, which
renders the constraints brief injected into the planner/writer/reviewer
prompts (see app/agents/prompts.py). It returns "" for `None`, which keeps
prompts for pre-existing courses byte-identical.

Sources: Blueprint sections 3.1-3.3 (archetype matrix, cognitive bridge,
analogy profiles) and Methodologies sections 1, 3.5 and 4 (segment
adaptation, MSTP writing style, Information Mapping).

Prompt-safety note: the rendered brief must never contain a line matching
`^\\s*\\d+\\. ` or the literal labels `COURSE TITLE:`, `CHAPTER TITLE:` or
`TEMPLATE KIND:`, because the offline mock AI client parses those out of
whatever prompt it is given (see app/services/mock_ai.py). Bullets only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.learner import AGE_GROUP_LABELS, EDU_LEVEL_LABELS, LearnerProfile


@dataclass(frozen=True)
class ArchetypeRules:
    """One row of the lookup matrix."""

    instructional_model: str
    structural_strategy: str
    output_mechanics: list[str]
    session_length: str
    blooms_emphasis: str
    assessment: str
    tone: str
    # Baseline slider positions the frontend snaps to when this age group is
    # picked (UAT 5.1 "Slider Coordination"), also used as the documented
    # default when a profile arrives with untouched sliders.
    default_chunk_word_cap: int
    default_jargon_density: float
    default_scaffolding_depth: float
    default_gamification_index: float
    # Segment-specific hard rules, stated as prohibitions where the client's
    # wording is a prohibition ("never harsh correction", "no intro definitions").
    hard_rules: list[str] = field(default_factory=list)


ARCHETYPES: dict[str, ArchetypeRules] = {
    "KG_PRIMARY": ArchetypeRules(
        instructional_model="Story-Based Pedagogy & Gamified Microlearning",
        structural_strategy=(
            "Anchor every abstract concept inside a linear narrative arc carried by "
            "recurring named characters. Piaget's concrete operational stage: concrete, "
            "sensory examples only, never abstract reasoning."
        ),
        output_mechanics=[
            "Keep each text block to 150-200 words.",
            "Write character screenplays/dialogue rather than exposition where possible.",
            "Interject gamified points, badges and streaks between activities.",
            "Change activity type frequently - never two long reading blocks in a row.",
        ],
        session_length="3-5 minute chunks",
        blooms_emphasis="Stay almost entirely at Remember and Understand.",
        assessment=(
            "Informal, embedded and low-stakes only: matching games, drag-and-drop, "
            "multiple choice with immediate encouraging feedback."
        ),
        tone="Warm storytelling with mascots/characters; positive-only feedback.",
        default_chunk_word_cap=200,
        default_jargon_density=0.0,
        default_scaffolding_depth=1.0,
        default_gamification_index=0.9,
        hard_rules=[
            "Never use idioms, sarcasm or abstract metaphor - language stays simple and literal.",
            "Never write harsh or negative correction; wrong answers get encouraging redirection.",
            "Never generate a formal scored test.",
            "Assume many learners are not yet independent readers: write text that reads aloud well.",
        ],
    ),
    "MIDDLE_SECONDARY": ArchetypeRules(
        instructional_model="Inquiry-Based Learning & Flipped Design",
        structural_strategy=(
            "Flip the traditional order: open with a mystery, anomaly or puzzling case "
            "that forces exploration, then supply the concepts, then a reflection prompt. "
            "Teens think increasingly abstractly (Piaget) and care about identity and "
            "peers (Erikson)."
        ),
        output_mechanics=[
            "Order every chapter as: the case mystery, then the concepts, then a reflection prompt.",
            "Drive explanation through questions the learner is invited to answer.",
            "Tie content to real-world relevance: careers, exams, and life outside school.",
            "Build in peer, social or discussion elements.",
        ],
        session_length="10-20 minute segments",
        blooms_emphasis=(
            "Use the full range, deliberately including Analyze, Evaluate and Create - "
            "do not cluster everything at Remember and Understand."
        ),
        assessment=(
            "Formal graded assessment is appropriate (quizzes, essays, projects); add "
            "self-assessment and reflection prompts."
        ),
        tone="Autonomy-supportive: treat the learner as increasingly capable of self-direction.",
        default_chunk_word_cap=450,
        default_jargon_density=0.35,
        default_scaffolding_depth=0.7,
        default_gamification_index=0.5,
        hard_rules=[
            "Never open a chapter with a definition - the anomaly or case comes first.",
            "Never run long unbroken text: mix media and activity types.",
        ],
    ),
    "HIGHER_ED": ArchetypeRules(
        instructional_model="Problem-Based Learning & Case Method",
        structural_strategy=(
            "Build modules around complex, open-ended business, legal or scientific data "
            "cases. A pedagogy-to-andragogy blend: growing self-direction inside a "
            "structured, externally accountable academic framework."
        ),
        output_mechanics=[
            "Open with raw data, a data table or a primary source - not with definitions.",
            "Favour independent inquiry structures: readings, primary sources, case studies.",
            "Sustain an academic register; content may be abstract and dense.",
            "Frame post-graduate material as seminar discussion and original contribution.",
        ],
        session_length="Full-length academic sessions; density is acceptable",
        blooms_emphasis=(
            "Full range for undergraduate; skew heavily to Analyze, Evaluate and Create "
            "for post-graduate."
        ),
        assessment=(
            "Formal and often high-stakes: essays, research papers, rubric-based grading "
            "with detailed feedback; add peer review and self-reflection at PG level."
        ),
        tone="Academic and impersonal; the learner is a scholar, not a pupil.",
        default_chunk_word_cap=650,
        default_jargon_density=0.7,
        default_scaffolding_depth=0.3,
        default_gamification_index=0.0,
        hard_rules=[
            "Never begin with introductory definitions of terms the syllabus assumes.",
            "Never pre-digest a case: leave the analysis to the learner.",
        ],
    ),
    "PROFESSIONAL": ArchetypeRules(
        instructional_model="Knowles' Andragogy & Scenario-Based Learning",
        structural_strategy=(
            "Focus entirely on task utility, operational ROI and workplace decision-making. "
            "Self-directed adults who bring prior experience as a resource and need the "
            "'why' before they will engage."
        ),
        output_mechanics=[
            "Use branching decision trees and realistic workplace scenarios.",
            "Simulate real artefacts: emails, chat messages, tickets, dashboards.",
            "State the on-the-job relevance in the opening lines, never at the end.",
            "Produce a companion job aid, checklist or quick-reference card per module.",
        ],
        session_length="Microlearning: 5-10 minute modules unless a longer programme is requested",
        blooms_emphasis="Pitch at Apply, Analyze and Evaluate - job application and behaviour change.",
        assessment=(
            "Emphasise Kirkpatrick Levels 3-4 (on-the-job behaviour, business results) over "
            "knowledge-recall quizzes alone."
        ),
        tone="Professional peer to professional peer; never a schoolroom tone.",
        default_chunk_word_cap=450,
        default_jargon_density=0.6,
        default_scaffolding_depth=0.4,
        default_gamification_index=0.1,
        hard_rules=[
            "Never bury 'why this matters' at the end of a section.",
            "Never use generic or academic examples where a workplace situation would do.",
        ],
    ),
}


# --- Panel B: the cognitive bridge -------------------------------------------


def domain_match_index(profile: LearnerProfile) -> float:
    """1.0 = Expert Track, 0.0 = Cross-Domain Bridge Track.

    Blank on either side means there is nothing to bridge *from*, so the
    course runs as a normal same-domain course rather than inventing analogies
    out of an unknown source domain.
    """
    source = profile.source_domain.strip().casefold()
    target = profile.target_domain.strip().casefold()
    if not source or not target:
        return 1.0
    if source in {"none", "n/a"}:
        return 1.0
    return 1.0 if source == target else 0.0


def is_cross_domain(profile: LearnerProfile) -> bool:
    return domain_match_index(profile) == 0.0


@dataclass(frozen=True)
class AnalogyProfile:
    """One of the client's named cross-domain translation profiles."""

    name: str
    pairs: list[str]


# Blueprint 3.2. Matched on keywords in the source/target domain text because
# the domain fields are free text. An unmatched cross-domain pair still gets
# the generic bridge rules below - it just has no pre-authored analogy set.
_ANALOGY_PROFILES: list[tuple[set[str], set[str], AnalogyProfile]] = [
    (
        {"stem", "science", "physics", "biology", "chemistry", "engineering", "math"},
        {"finance", "business", "commerce", "economics", "accounting", "marketing"},
        AnalogyProfile(
            name="Science native learning business/finance",
            pairs=[
                "Explain market liquidity and cash flow through viscosity and hydrodynamic "
                "pressure: cash moving through corporate accounts behaves like water through "
                "pipes, and illiquid assets behave like high-viscosity sludge.",
                "Explain diversification and portfolio risk as genetic variance and ecosystem "
                "resilience: spreading capital across industries protects a portfolio from a "
                "market shock exactly as biodiversity protects an ecosystem from a single "
                "disease outbreak.",
            ],
        ),
    ),
    (
        {"business", "commerce", "finance", "management", "sales", "marketing"},
        {"software", "software_eng", "engineering", "stem", "technical", "data", "it", "computer"},
        AnalogyProfile(
            name="Business native learning technical/STEM",
            pairs=[
                "Explain object-oriented programming as franchise corporate frameworks: a class "
                "is the master franchise blueprint, an object is one physical store, and "
                "inheritance is a local store adopting regional corporate policy while adding "
                "its own local menu items.",
                "Explain API integrations as notarised commercial treaties or service-level "
                "agreements between two businesses.",
            ],
        ),
    ),
]


def analogy_profile(profile: LearnerProfile) -> AnalogyProfile | None:
    """The pre-authored analogy set for this domain pair, if one matches."""
    if not is_cross_domain(profile):
        return None
    source = _tokens(profile.source_domain)
    target = _tokens(profile.target_domain)
    for source_keys, target_keys, analogies in _ANALOGY_PROFILES:
        if source & source_keys and target & target_keys:
            return analogies
    return None


def _tokens(value: str) -> set[str]:
    cleaned = "".join(char if char.isalnum() else " " for char in value.casefold())
    return {token for token in cleaned.split() if token}


# --- Bloom's taxonomy (Methodologies model 2) --------------------------------

BLOOM_VERBS: dict[str, list[str]] = {
    "Remember": ["define", "list", "name", "identify", "recall", "label", "state", "match"],
    "Understand": [
        "describe", "explain", "summarize", "classify", "discuss", "paraphrase", "interpret",
    ],
    "Apply": ["apply", "demonstrate", "use", "implement", "solve", "execute", "illustrate"],
    "Analyze": ["compare", "differentiate", "organize", "examine", "categorize", "deconstruct"],
    "Evaluate": ["assess", "critique", "justify", "recommend", "appraise", "defend", "prioritize"],
    "Create": ["design", "construct", "develop", "formulate", "build", "compose", "generate"],
}

BLOOM_ASSESSMENTS: dict[str, str] = {
    "Remember": "recall quiz, flashcard or matching",
    "Understand": "short answer or discussion prompt",
    "Apply": "guided simulation or applied exercise",
    "Analyze": "branching scenario or sorting/classification activity",
    "Evaluate": "case study requiring justification",
    "Create": "open-ended project or assignment",
}

# Unmeasurable verbs that may never appear in a learning objective.
BANNED_OBJECTIVE_VERBS: tuple[str, ...] = (
    "understand",
    "know",
    "learn",
    "appreciate",
    "be familiar with",
    "be aware of",
    "grasp",
)


# --- Information Mapping (Methodologies section 4) ---------------------------

INFORMATION_TYPES: dict[str, str] = {
    "Procedure": "numbered steps, one action per step, imperative mood",
    "Process": "sequential stages in order; a flow diagram once there are more than 3 stages",
    "Concept": "definition, key characteristics, at least one example and one non-example",
    "Fact": "a table or list, never elaborated into narrative prose",
    "Structure": "a diagram, part list or hierarchy - never a paragraph description",
    "Classification": "a comparison table or decision tree",
    "Principle": "the rule, then the rationale, then an example",
}


# --- brief rendering ---------------------------------------------------------


def _slider(label: str, value: float, low: str, high: str) -> str:
    return f"- {label}: {value:.2f} on a 0.00 ({low}) to 1.00 ({high}) scale."


def learner_brief(profile: LearnerProfile | None) -> str:
    """The constraints brief, or "" when no profile is set.

    Returning "" (no trailing newline) is what keeps prompts for courses
    created before this feature byte-identical - see the prompt-invariance
    tests in backend/tests/test_memory_prompts.py.
    """
    if profile is None:
        return ""

    rules = ARCHETYPES[profile.age_group]
    cross_domain = is_cross_domain(profile)
    lines: list[str] = []

    lines.append("LEARNER PROFILE AND MANDATORY INSTRUCTIONAL CONSTRAINTS:")
    lines.append(f"- Learner archetype: {AGE_GROUP_LABELS[profile.age_group]}.")
    lines.append(f"- Highest educational attainment: {EDU_LEVEL_LABELS[profile.edu_level]}.")
    lines.append(f"- Instructional model to apply: {rules.instructional_model}.")
    lines.append(f"- Structural strategy: {rules.structural_strategy}")
    lines.append(f"- Session length: {rules.session_length}.")
    lines.append(f"- Tone: {rules.tone}")
    lines.append(f"- Bloom's emphasis: {rules.blooms_emphasis}")
    lines.append(f"- Assessment style: {rules.assessment}")

    lines.append("")
    lines.append("REQUIRED OUTPUT MECHANICS:")
    lines.extend(f"- {item}" for item in rules.output_mechanics)
    if rules.hard_rules:
        lines.extend(f"- {item}" for item in rules.hard_rules)

    lines.append("")
    lines.append("STYLE TUNING (obey these positions exactly):")
    lines.append(
        _slider("Jargon density", profile.jargon_density, "plain language", "industry native")
    )
    lines.append(
        _slider(
            "Scaffolding depth",
            profile.scaffolding_depth,
            "independent case studies",
            "step-by-step guidance",
        )
    )
    lines.append(
        _slider(
            "Gamification",
            profile.gamification_index,
            "strictly academic",
            "deep story-driven",
        )
    )
    lines.append(
        f"- Delivery chunk size: no single text block may exceed {profile.chunk_word_cap} words. "
        "Split longer material into separate blocks with their own headings."
    )
    if profile.manual_override:
        lines.append(
            "- These slider positions were hand-tuned by the instructional designer and "
            "override the archetype baseline. Follow them as given."
        )

    lines.append("")
    if cross_domain:
        lines.append(
            f"COGNITIVE BRIDGE TRACK (domain match index 0.0): the learner's native domain is "
            f"{profile.source_domain.strip()} and this course teaches "
            f"{profile.target_domain.strip()}. Map every foreign concept onto the familiar "
            "parent domain using rigorous analogies (Cognitive Apprenticeship & Scaffolding):"
        )
        lines.append(
            "- Introduce each new concept through an analogy drawn from the learner's native "
            "domain before giving the target-domain definition."
        )
        lines.append(
            '- Wrap every technical keyword in a functional tag: <jargon term="...">the '
            "term</jargon>, so the player can surface a definition on demand."
        )
        lines.append(
            "- State explicitly where each analogy breaks down; never let a mapping imply "
            "something false about the target domain."
        )
        matched = analogy_profile(profile)
        if matched is not None:
            lines.append(f"- Pre-authored analogy set ({matched.name}) - use these where they fit:")
            lines.extend(f"  - {pair}" for pair in matched.pairs)
    else:
        lines.append(
            "EXPERT TRACK (domain match index 1.0): the learner already works in this domain. "
            "Optimise for cognitive load and time-to-value:"
        )
        lines.append("- Skip baseline summaries and foundational definitions entirely.")
        lines.append("- Never open with an introductory analogy; go straight to application.")
        lines.append("- Lead with the formula, the code or the decision rule, then explain it.")
        lines.append("- Use high-level industry terminology immediately, without glossing it.")

    return "\n".join(lines)


def pedagogy_rules_brief(profile: LearnerProfile | None) -> str:
    """Methodology and house-style rules (Information Mapping, Bloom's verb
    bank, MSTP writing style). Separate from `learner_brief` because the
    writer and reviewer need it while the planner mostly does not.
    """
    if profile is None:
        return ""

    lines: list[str] = []
    lines.append("INFORMATION MAPPING - classify every chunk as exactly one type and use its structure:")
    lines.extend(f"- {name}: {structure}." for name, structure in INFORMATION_TYPES.items())
    lines.append(
        "- Give every chunk a descriptive heading that states what it contains, before the "
        "content itself."
    )
    lines.append(
        "- One purpose per chunk: never mix a procedure with background concept explanation."
    )
    lines.append(
        "- Overview first, detail second, so a reader can stop at the depth they need."
    )
    lines.append(
        "- For a Structure, Process or Classification chunk the diagram or table is the primary "
        "vehicle - the text supports it rather than replacing it."
    )

    lines.append("")
    lines.append("LEARNING OBJECTIVES - Bloom's taxonomy rules:")
    lines.append(
        "- Write each objective as: the learner will be able to [verb] [specific task] "
        "[condition or context]."
    )
    lines.append(
        "- Use exactly one verb per objective, taken from the approved bank: "
        + "; ".join(f"{level} - {', '.join(verbs)}" for level, verbs in BLOOM_VERBS.items())
        + "."
    )
    lines.append(
        "- These verbs are banned because they cannot be measured: "
        + ", ".join(BANNED_OBJECTIVE_VERBS)
        + "."
    )
    lines.append(
        "- Match the assessment to the level: "
        + "; ".join(f"{level} - {kind}" for level, kind in BLOOM_ASSESSMENTS.items())
        + "."
    )
    lines.append(
        "- Sequence lower levels before the higher levels that depend on them."
    )

    lines.append("")
    lines.append("WRITING STYLE (Microsoft Writing Style Guide house rules):")
    lines.append("- Use the serial comma, present tense and active voice.")
    lines.append('- Address the learner as "you"; use "we" sparingly.')
    lines.append("- Use imperative mood for every instruction or step.")
    lines.append(
        '- Never write "should" for a required action - the learner must do something, or the '
        'system will do something. Reserve "should" for genuinely optional guidance.'
    )
    lines.append('- Never write "e.g." or "i.e." - write "for example" or "that is".')
    lines.append(
        '- Structure each step as context then action: "In the Settings menu, select '
        'Notifications", never the reverse.'
    )
    lines.append("- One action per step.")
    lines.append('- Write "click", never "click on".')
    lines.append('- Separate consecutive navigation with >, as in "select File > Save As".')
    lines.append('- Never write "please" or "thank you" in instructional copy.')

    lines.append("")
    lines.append("ACCESSIBILITY (WCAG 2.1 AA, built in rather than reviewed later):")
    lines.append(
        "- Every image, diagram and table needs a written alt-text description; mark purely "
        "decorative images as decorative instead of inventing a description."
    )
    lines.append("- Use a proper heading hierarchy with no skipped levels.")
    lines.append(
        "- Describe a keyboard-operable equivalent for any interactive element you propose."
    )

    return "\n".join(lines)


def slider_defaults(age_group: str) -> dict[str, float | int]:
    """The baseline slider positions for an age group (UAT 5.1). Shared with
    the frontend through `GET /api/pedagogy/defaults`."""
    rules = ARCHETYPES[age_group]
    return {
        "jargon_density": rules.default_jargon_density,
        "scaffolding_depth": rules.default_scaffolding_depth,
        "gamification_index": rules.default_gamification_index,
        "chunk_word_cap": rules.default_chunk_word_cap,
    }
