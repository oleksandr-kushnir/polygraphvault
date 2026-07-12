from app.filters import in_scope, is_excluded, is_hidden, is_included, parse_csv


def test_csv_and_extension_scope():
    assert parse_csv("md, pdf, ,TXT") == ["md", "pdf", "TXT"]
    assert is_included("Docs/Guide.MD", ["md"])
    assert not is_included("Docs/image.png", ["md", "pdf"])
    assert not is_included("README", [])
    assert is_included("recording.wav", [])


def test_hidden_and_excludes():
    assert is_hidden(".obsidian/config.json")
    assert is_hidden("docs/.private/note.md")
    assert is_excluded("docs/node_modules/a.js", ["node_modules/"])
    assert is_excluded("docs/draft.tmp", ["*.tmp"])
    assert not in_scope(".private.md", "md", False, "")
    assert not in_scope("docs/draft.md", "md", True, "draft.*")
    assert in_scope("docs/final.md", "md", False, "draft.*")
