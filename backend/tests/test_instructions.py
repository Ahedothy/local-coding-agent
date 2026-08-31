from pathlib import Path

from coding_agent.agent.instructions import load_workspace_instructions


def test_load_root_agents_md_with_bound_and_metadata(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Rules\n" + "x" * 30, encoding="utf-8")

    instructions = load_workspace_instructions(tmp_path, max_chars=12)

    assert instructions.found is True
    assert instructions.path == "AGENTS.md"
    assert instructions.injected_chars == 12
    assert instructions.truncated is True
    assert instructions.prompt_fragment().startswith("\n\n<workspace_instructions")
    assert "truncated" in instructions.prompt_fragment()


def test_missing_agents_md_is_noop(tmp_path: Path) -> None:
    instructions = load_workspace_instructions(tmp_path)

    assert instructions.found is False
    assert instructions.injected_chars == 0
    assert instructions.prompt_fragment() == ""
