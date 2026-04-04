"""Tests for ResourceLoader — shared file discovery and parsing."""

from agent_cli.resource_loader import ResourceLoader


class TestParseFile:
    def test_with_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\nname: hello\ndescription: world\n---\n\nBody here")
        r = ResourceLoader._parse_file(f)
        assert r is not None
        assert r.name == "hello"
        assert r.meta["description"] == "world"
        assert r.body == "Body here"

    def test_without_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Just markdown\n\nNo frontmatter here.")
        r = ResourceLoader._parse_file(f)
        assert r is not None
        assert r.name == "test"
        assert r.meta == {}
        assert "Just markdown" in r.body

    def test_name_from_filename(self, tmp_path):
        f = tmp_path / "my-skill.md"
        f.write_text("---\ndescription: no name field\n---\n\nBody")
        r = ResourceLoader._parse_file(f)
        assert r.name == "my-skill"

    def test_name_from_dir_for_skill_md(self, tmp_path):
        d = tmp_path / "cool-skill"
        d.mkdir()
        f = d / "SKILL.md"
        f.write_text("---\ndescription: dir skill\n---\n\nBody")
        r = ResourceLoader._parse_file(f)
        assert r.name == "cool-skill"

    def test_empty_body_returns_none(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("---\nname: empty\n---\n\n")
        r = ResourceLoader._parse_file(f)
        assert r is None

    def test_nonexistent_file(self, tmp_path):
        r = ResourceLoader._parse_file(tmp_path / "nope.md")
        assert r is None

    def test_source_path_recorded(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("content here")
        r = ResourceLoader._parse_file(f)
        assert str(f) in r.source_path


class TestLoadAll:
    def test_single_path(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        (d / "a.md").write_text("---\nname: a\n---\n\nBody A")
        (d / "b.md").write_text("---\nname: b\n---\n\nBody B")

        loader = ResourceLoader([d])
        results = loader.load_all()
        assert "a" in results
        assert "b" in results
        assert len(results) == 2

    def test_priority_override(self, tmp_path):
        high = tmp_path / "high"
        low = tmp_path / "low"
        high.mkdir()
        low.mkdir()

        (high / "same.md").write_text("---\nname: same\n---\n\nHigh priority")
        (low / "same.md").write_text("---\nname: same\n---\n\nLow priority")

        loader = ResourceLoader([high, low])
        results = loader.load_all()
        assert "same" in results
        assert "High priority" in results["same"].body

    def test_lower_priority_fills_gaps(self, tmp_path):
        high = tmp_path / "high"
        low = tmp_path / "low"
        high.mkdir()
        low.mkdir()

        (high / "only-high.md").write_text("---\nname: only-high\n---\n\nHigh")
        (low / "only-low.md").write_text("---\nname: only-low\n---\n\nLow")

        loader = ResourceLoader([high, low])
        results = loader.load_all()
        assert "only-high" in results
        assert "only-low" in results

    def test_nonexistent_path_skipped(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.md").write_text("---\nname: a\n---\n\nBody")

        loader = ResourceLoader([tmp_path / "fake", real])
        results = loader.load_all()
        assert "a" in results

    def test_dir_entry(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        sub = d / "my-tool"
        sub.mkdir()
        (sub / "SKILL.md").write_text("---\nname: my-tool\n---\n\nTool body")
        (d / "flat.md").write_text("---\nname: flat\n---\n\nFlat body")

        loader = ResourceLoader([d], dir_entry="SKILL.md")
        results = loader.load_all()
        assert "my-tool" in results
        assert "flat" in results


class TestLoadOne:
    def test_found(self, tmp_path):
        d = tmp_path / "agents"
        d.mkdir()
        (d / "explorer.md").write_text("---\nname: explorer\n---\n\nExplorer body")

        loader = ResourceLoader([d])
        r = loader.load_one("explorer")
        assert r is not None
        assert r.name == "explorer"

    def test_not_found(self, tmp_path):
        d = tmp_path / "agents"
        d.mkdir()

        loader = ResourceLoader([d])
        r = loader.load_one("nonexistent")
        assert r is None

    def test_priority_first_match(self, tmp_path):
        high = tmp_path / "high"
        low = tmp_path / "low"
        high.mkdir()
        low.mkdir()

        (high / "x.md").write_text("---\nname: x\n---\n\nHigh")
        (low / "x.md").write_text("---\nname: x\n---\n\nLow")

        loader = ResourceLoader([high, low])
        r = loader.load_one("x")
        assert "High" in r.body

    def test_fallback_to_lower_path(self, tmp_path):
        high = tmp_path / "high"
        low = tmp_path / "low"
        high.mkdir()
        low.mkdir()

        (low / "only-low.md").write_text("---\nname: only-low\n---\n\nLow body")

        loader = ResourceLoader([high, low])
        r = loader.load_one("only-low")
        assert r is not None
        assert "Low body" in r.body

    def test_dir_entry_lookup(self, tmp_path):
        d = tmp_path / "skills"
        d.mkdir()
        sub = d / "team-creator"
        sub.mkdir()
        (sub / "SKILL.md").write_text("---\nname: team-creator\n---\n\nTeam body")

        loader = ResourceLoader([d], dir_entry="SKILL.md")
        r = loader.load_one("team-creator")
        assert r is not None
        assert "Team body" in r.body


class TestListNames:
    def test_returns_all_names(self, tmp_path):
        d = tmp_path / "res"
        d.mkdir()
        (d / "a.md").write_text("body a")
        (d / "b.md").write_text("body b")
        (d / "c.md").write_text("body c")

        loader = ResourceLoader([d])
        names = loader.list_names()
        assert set(names) == {"a", "b", "c"}

    def test_deduped(self, tmp_path):
        high = tmp_path / "high"
        low = tmp_path / "low"
        high.mkdir()
        low.mkdir()
        (high / "x.md").write_text("high x")
        (low / "x.md").write_text("low x")
        (low / "y.md").write_text("low y")

        loader = ResourceLoader([high, low])
        names = loader.list_names()
        assert "x" in names
        assert "y" in names
        assert names.count("x") == 1
