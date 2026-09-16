"""Display projection of a context-only study-setting span.

Chapter 2 dropped a whole study-setting span when a figure, table or
citation locator occurred anywhere inside it. That filter deleted 36 percent
of the eligibility spans and starved the writer of the study setting (chapter
2 yield audit, section 4.1, findings E4, FS-1 and W1).

Contract ``question-context-redacted-evidence-v2`` keeps the sentence and
erases only the pointer. The redaction runs on the display and comparison
projection. It never changes the stored span bytes or their hash, so custody
still binds to the raw chunk text. A sentence whose meaning depends on an
unseen figure or table still dies on ``RESIDUAL_LOCATOR_PATTERN``.

``validation.py`` and ``generation.py`` both import this module, so the
validator can re-derive the projection from the stored bytes.
"""

from __future__ import annotations

import re

MIN_CONTEXT_ONLY_SPAN_CHARS = 16

# ``[8]``, ``[3, 4]``, ``[12-15]``.
BRACKET_CITATION_PATTERN = re.compile(r"\[\s*\d+(?:\s*[;,–-]\s*\d+)*\s*\]")
# One parenthetical without nested brackets. Only a parenthetical that carries
# a locator word with a number, or an author-year pair, is a pointer.
PARENTHETICAL_PATTERN = re.compile(r"\((?:[^()]{0,200})\)")
LOCATOR_WORD_PATTERN = re.compile(
    r"\b(?:fig(?:ure|s)?|tab(?:le|s)?|eq(?:uation|s)?|suppl(?:ementary|ement)?|"
    r"sect(?:ion)?|panel|appendix)\b\.?\s*(?:S?\d|[IVX]+\b)",
    re.IGNORECASE,
)
AUTHOR_YEAR_PATTERN = re.compile(
    r"(?:e\.g\.,?\s*)?[A-Z][^\W\d_]+(?:\s+(?:et\s+al\.?|and\s+[A-Z][^\W\d_]+|"
    r"&\s+[A-Z][^\W\d_]+))?,?\s*\d{4}[a-z]?"
)
# The locator half of the chapter 2 filter, without the citation alternatives.
# A sentence such as "Table 2 lists the twelve stations" still points the
# reader at material the reader cannot see, so it is never displayed.
RESIDUAL_LOCATOR_PATTERN = re.compile(
    r"\b(?:fig(?:ure|s)?|tab(?:le|s)?|eq(?:uation|s)?|suppl(?:ementary|ement)?|"
    r"sect(?:ion)?|panel|appendix)\s*\.?\s*(?:[0-9]|[IVX]+\b|S[0-9])",
    re.IGNORECASE,
)
# Two-column PDF extraction joins the neighbouring column with a long space run.
COLUMN_INTERLEAVE_PATTERN = re.compile(r"[ \t]{8,}")
# A study-setting sentence is complete when it carries one finite verb and
# ends in terminal punctuation. The verb list is deliberately wide: a false
# "no verb" deletes context the writer needs, a false "verb" only forwards a
# fragment that the writer already cannot quote.
_SETTING_FINITE_VERB_PATTERN = re.compile(
    r"\b(?:is|are|was|were|has|have|had|do|does|did|can|could|will|would|"
    r"show(?:s|ed|n)?|report(?:s|ed)?|increas(?:e|es|ed)|decreas(?:e|es|ed)|"
    r"declin(?:e|es|ed)|rang(?:e|es|ed)|occur(?:s|red)?|reach(?:es|ed)?|"
    r"exceed(?:s|ed)?|averag(?:e|es|ed)|remain(?:s|ed)?|var(?:y|ies|ied)|"
    r"contribut(?:e|es|ed)|represent(?:s|ed)?|indicat(?:e|es|ed)|found|"
    r"observed|measured|estimated|account(?:s|ed)?|correlat(?:e|es|ed)|"
    r"differ(?:s|ed)?|took|take(?:s|n)?|collect(?:s|ed)?|sampl(?:e|es|ed)|"
    r"conduct(?:s|ed)?|perform(?:s|ed)?|carr(?:y|ies|ied)|locat(?:e|es|ed)|"
    r"situat(?:e|es|ed)|us(?:e|es|ed)|deploy(?:s|ed)?|obtain(?:s|ed)?|"
    r"record(?:s|ed)?|analys(?:e|es|ed)|analyz(?:e|es|ed)|includ(?:e|es|ed)|"
    r"consist(?:s|ed)?|cover(?:s|ed)?|span(?:s|ned)?|last(?:s|ed)?|"
    r"began|begin(?:s)?|start(?:s|ed)?|end(?:s|ed)?|ran|run(?:s)?|"
    r"isolat(?:e|es|ed)|deriv(?:e|es|ed)|defin(?:e|es|ed)|denot(?:e|es|ed)|"
    r"refer(?:s|red)?|mean(?:s|t)?|stand(?:s)?|compris(?:e|es|ed)|"
    r"lie(?:s)?|lay|extend(?:s|ed)?|belong(?:s|ed)?|occupi(?:es|ed)|"
    r"receiv(?:e|es|ed)|appl(?:y|ies|ied)|select(?:s|ed)?|"
    r"describ(?:e|es|ed)|operat(?:e|es|ed)|driv(?:e|es|en)|drove|"
    r"install(?:s|ed)?|mount(?:s|ed)?|equip(?:s|ped)?|fitted|held|hold(?:s)?|"
    r"flew|fl(?:y|ies)|made|make(?:s)?|gave|give(?:s)?|placed|kept|keep(?:s)?|"
    r"stood|stand(?:s)?|grew|grow(?:s)?|form(?:s|ed)?|sail(?:s|ed)?|"
    r"visit(?:s|ed)?|travel(?:s|led|ed)?|arriv(?:e|es|ed)|left|leave(?:s)?|"
    r"return(?:s|ed)?|follow(?:s|ed)?|continu(?:e|es|ed)|complet(?:e|es|ed)|"
    r"determin(?:e|es|ed)|calculat(?:e|es|ed)|comput(?:e|es|ed)|"
    r"identif(?:y|ies|ied)|characteri[sz](?:e|es|ed)|process(?:es|ed)?|"
    r"stor(?:e|es|ed)|prepar(?:e|es|ed)|treat(?:s|ed)?|expos(?:e|es|ed)|"
    r"incubat(?:e|es|ed)|cultur(?:e|es|ed)|grown|sequenc(?:e|es|ed)|"
    r"extract(?:s|ed)?|filter(?:s|ed)?|weigh(?:s|ed)?|count(?:s|ed)?|"
    r"provid(?:e|es|ed)|suppl(?:y|ies|ied)|yield(?:s|ed)?|produc(?:e|es|ed))\b",
    re.IGNORECASE,
)
_TERMINAL_PUNCTUATION_PATTERN = re.compile(r"[.!?][\"'”’)\]]*\s*$")


def redact_locators(text: str) -> str:
    """Erase citation and locator pointers. Keep the sentence.

    Only characters are removed. A deleted ``(Figure 1)`` or ``[8]`` cannot
    add a claim, a place, a period or an answer to the sentence.
    """
    value = BRACKET_CITATION_PATTERN.sub("", text)
    value = PARENTHETICAL_PATTERN.sub(
        lambda match: (
            ""
            if LOCATOR_WORD_PATTERN.search(match.group())
            or AUTHOR_YEAR_PATTERN.search(match.group())
            else match.group()
        ),
        value,
    )
    value = re.sub(r"\(\s*\)|\[\s*\]", "", value)
    # A pointer that sat between a word and its punctuation leaves a space.
    value = re.sub(r"[ \t]+([,.;:)])", r"\1", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def is_complete_setting_sentence(text: str) -> bool:
    """Return whether displayed text is at least one complete sentence."""
    stripped = text.strip()
    if len(stripped) < MIN_CONTEXT_ONLY_SPAN_CHARS:
        return False
    if not _TERMINAL_PUNCTUATION_PATTERN.search(stripped):
        return False
    return bool(_SETTING_FINITE_VERB_PATTERN.search(stripped))


def context_only_display_text(text: str) -> str | None:
    """Return the display projection of one span, or None when unusable.

    The span is unusable when the raw bytes carry a two-column join, when the
    redacted text still points at a figure or table, or when the redacted
    text is not a complete sentence of at least 16 characters.
    """
    if not isinstance(text, str) or COLUMN_INTERLEAVE_PATTERN.search(text):
        return None
    displayed = redact_locators(text)
    if RESIDUAL_LOCATOR_PATTERN.search(displayed):
        return None
    if not is_complete_setting_sentence(displayed):
        return None
    return displayed
