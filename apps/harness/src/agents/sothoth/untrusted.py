import re


def untrusted(tag: str, text: str) -> str:
    """text inside <tag untrusted>, with any closing tag in it escaped"""
    escaped = re.sub(f"</({tag})", r"&lt;/\1", text, flags=re.IGNORECASE)
    return f"<{tag} untrusted>\n{escaped}\n</{tag}>"
