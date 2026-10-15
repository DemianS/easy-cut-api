from services.prompt_loader import load_prompt


def test_prompt_version_changes_with_content(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("services.prompt_loader.PROMPTS_PATH", tmp_path)
    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("первая версия", encoding="utf-8")

    first = load_prompt("prompt.txt")
    prompt_path.write_text("вторая версия", encoding="utf-8")
    second = load_prompt("prompt.txt")

    assert first.text == "первая версия"
    assert second.text == "вторая версия"
    assert first.version != second.version
