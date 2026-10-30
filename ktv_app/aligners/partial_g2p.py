"""Keep convertible context around reported pronunciation failures, in source order."""


def convert_parts(text, convert, error_type, depth=0):
    """Return (word, omitted_text, reason) records without replacing displayed lyrics."""
    try:
        return [(word, None, None) for word in convert(text)]
    except error_type as error:
        tokens = [
            t for t in getattr(error, "unconverted_tokens", []) if t and t in text
        ]
        if not tokens or depth >= 16:
            return [(None, text, str(error))]
        # Partition at the earliest reported unknown, preserving source order,
        # including repeats; remaining phrases retain all available context.
        token = min(tokens, key=lambda t: text.find(t))
        at = text.find(token)
        result = []
        if text[:at].strip():
            result.extend(convert_parts(text[:at], convert, error_type, depth + 1))
        result.append((None, token, str(error)))
        if text[at + len(token) :].strip():
            result.extend(
                convert_parts(text[at + len(token) :], convert, error_type, depth + 1)
            )
        return result


def convert_scat_parts(text, convert, error_type, compose):
    """Use known scat readings for G2P while retaining the displayed source token.

    Do not interpret arbitrary English as Mandarin. If the given reading cannot
    be represented by G2P, fall back to normal conversion of the original text.
    """
    from .scat_pronunciation import pronunciation_lyrics

    _, _, changes = pronunciation_lyrics(text)
    if not changes:
        return convert_parts(text, convert, error_type)
    result = []
    cursor = 0
    for change in changes:
        begin, end = change["source_start"], change["source_end"]
        if begin > cursor:
            result.extend(convert_parts(text[cursor:begin], convert, error_type))
        reading = convert_parts(change["pronunciation"], convert, error_type)
        if reading and all(word is not None for word, _, _ in reading):
            combined = compose(change["display"], [word for word, _, _ in reading])
            if combined is not None:
                result.append((combined, None, None))
            else:
                result.extend(convert_parts(change["display"], convert, error_type))
        else:
            result.extend(convert_parts(change["display"], convert, error_type))
        cursor = end
    if cursor < len(text):
        result.extend(convert_parts(text[cursor:], convert, error_type))
    return result
