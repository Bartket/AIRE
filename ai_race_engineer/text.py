"""One way to flatten a spoken question into words.

Five modules were doing this by hand, and two of them have to agree for the
evals to mean anything: `llm._direct_telemetry_answer` decides from the
normalised question whether a turn is answered deterministically, and
`evals.grade_record` decides from the same string what that turn should have
said. A copy that drifts does not fail loudly — it grades a route that was
never taken.
"""


def normalise_question(text) -> str:
    """Lower-case words, single-spaced, punctuation dropped.

    Speech recognition supplies the punctuation, not the driver, so it
    carries no intent: "Say again?" and "say again" are one question.

    Apostrophes are removed rather than turned into spaces, so a contraction
    stays one word. Split, "How's my fuel?" became "how s my fuel" while every
    phrase table spells it "hows my fuel" — so the most-asked question on the
    radio never reached its deterministic answer whenever the recogniser kept
    the apostrophe, and it does. Both the straight and the curly one, because
    recognisers emit either.
    """
    joined = str(text or "").lower().replace("'", "").replace("’", "")
    return " ".join(
        "".join(ch if ch.isalnum() else " " for ch in joined).split())


def question_words(text) -> set:
    """The same normalisation, as a set of words for topic matching."""
    return set(normalise_question(text).split())
