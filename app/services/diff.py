import difflib


def generate_diff(old_text: str, new_text: str) -> str:
    old_lines = (old_text or "").splitlines(keepends=True)
    new_lines = (new_text or "").splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="running-config",
        tofile="rendered-template",
        lineterm="",
    )
    return "\n".join(diff_lines)
