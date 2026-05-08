"""Tests for Gate 4.7 — README enhancement gate."""
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gates.readme import ReadmeGate, BANANA_MAKER_SCRIPT
from config import Config
from db import StateDB
from models import PublishJob


@pytest.fixture
def readme_gate(test_config, in_memory_db, temp_audit_log):
    """ReadmeGate with in-memory DB and no real LLM client."""
    from audit import AuditLogger
    audit = AuditLogger(log_path=str(temp_audit_log))
    gate = ReadmeGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
    # Override client to None so tests don't need DEEPINFRA_API_KEY
    gate.client = None
    return gate


@pytest.fixture
def sample_project(tmp_path):
    """A minimal project directory with git init."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "README.md").write_text("# Placeholder")
    (tmp_path / "main.py").write_text("print('hello')")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_hello(): pass")
    return tmp_path


@pytest.fixture
def published_job(sample_project):
    """A PublishJob that looks published."""
    return PublishJob(
        build_job_id="metroplex-ideaforge-99",
        title="Test Tool",
        repo_name="test-tool",
        repo_url="https://github.com/m2ai-portfolio/test-tool",
        status="published",
        project_dir=str(sample_project),
        created_at=datetime.now(),
        published_at=datetime.now(),
    )


class TestReadmeGateInit:
    """Test ReadmeGate initialization."""

    def test_initializes_without_api_key(self, test_config, in_memory_db, temp_audit_log):
        """Gate initializes even without DEEPINFRA_API_KEY (client will be None)."""
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        with patch.dict("os.environ", {"DEEPINFRA_API_KEY": ""}, clear=False):
            gate = ReadmeGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
            assert gate.client is None

    def test_initializes_with_api_key(self, test_config, in_memory_db, temp_audit_log):
        """Gate initializes OpenAI client when key is set."""
        from audit import AuditLogger
        audit = AuditLogger(log_path=str(temp_audit_log))
        with patch.dict("os.environ", {"DEEPINFRA_API_KEY": "test-key-123"}, clear=False):
            gate = ReadmeGate(config=test_config, state_db=in_memory_db, audit_logger=audit)
            assert gate.client is not None


class TestDryRun:
    """Test dry_run mode doesn't modify anything."""

    def test_dry_run_returns_pending(self, readme_gate, published_job):
        """Dry run returns pending status without modifying the DB."""
        results = readme_gate.run(published_jobs=[published_job], dry_run=True)
        assert len(results) == 1
        assert results[0]["status"] == "pending"
        assert results[0]["build_job_id"] == "metroplex-ideaforge-99"

    def test_dry_run_no_db_records(self, readme_gate, published_job, in_memory_db):
        """Dry run doesn't create readme_jobs records."""
        readme_gate.run(published_jobs=[published_job], dry_run=True)
        assert not in_memory_db.has_readme("metroplex-ideaforge-99")

    def test_dry_run_skips_already_enhanced(self, readme_gate, published_job, in_memory_db):
        """Dry run skips jobs that already have a completed readme."""
        in_memory_db.record_readme_job(
            build_job_id="metroplex-ideaforge-99",
            repo_url="https://github.com/m2ai-portfolio/test-tool",
            status="completed",
        )
        results = readme_gate.run(published_jobs=[published_job], dry_run=True)
        assert len(results) == 0


class TestReadmeContentGeneration:
    """Test README content generation prompt construction."""

    def test_generate_readme_prompt_includes_title(self, readme_gate):
        """Prompt includes the project title."""
        readme_gate.client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "# Test Tool\n\nA test tool."
        readme_gate.client.chat.completions.create.return_value = mock_response

        result = readme_gate._generate_readme_content(
            spec_text="Test spec content",
            file_tree="test-tool/\n├── main.py",
            title="Test Tool",
        )

        call_args = readme_gate.client.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "Test Tool" in user_msg
        assert "Test spec content" in user_msg
        assert "main.py" in user_msg

    def test_strips_code_fence_wrapper(self, readme_gate):
        """LLM output wrapped in code fences gets cleaned."""
        readme_gate.client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "```markdown\n# Title\n\nContent\n```"
        readme_gate.client.chat.completions.create.return_value = mock_response

        result = readme_gate._generate_readme_content("spec", "tree", "Title")
        assert not result.startswith("```")
        assert not result.endswith("```")
        assert "# Title" in result


class TestInfographicCommand:
    """Test infographic command construction."""

    def test_command_includes_all_args(self, readme_gate):
        """Built command includes all required arguments."""
        cmd = readme_gate.build_infographic_command(
            title="Test Tool",
            value_prop="A CLI tool for automating dev workflows",
            output_path="/tmp/infographic.png",
        )
        assert "python3" in cmd[0]
        assert str(BANANA_MAKER_SCRIPT) in cmd[1]
        assert "--model" in cmd
        assert "flash" in cmd
        assert "--output" in cmd
        assert "/tmp/infographic.png" in cmd
        assert "--aspect-ratio" in cmd
        assert "16:9" in cmd

    def test_prompt_includes_value_prop_as_life_scene(self, readme_gate):
        """The prompt uses value prop as life-scene brief (story-driven style)."""
        cmd = readme_gate.build_infographic_command(
            title="My Tool",
            value_prop="search your notes from the terminal",
            output_path="/tmp/out.png",
        )
        # The prompt is cmd[2]
        assert "search your notes from the terminal" in cmd[2]
        # Life-scene style guarantees: person, real setting, empathetic mood
        assert "person" in cmd[2].lower()
        assert "real setting" in cmd[2].lower()
        assert "empathetic" in cmd[2].lower()


class TestFileTree:
    """Test file tree building."""

    def test_builds_tree(self, readme_gate, sample_project):
        """File tree includes project files."""
        tree = readme_gate._build_file_tree(sample_project)
        assert "main.py" in tree
        assert "tests" in tree

    def test_excludes_git(self, readme_gate, sample_project):
        """File tree excludes .git directory."""
        tree = readme_gate._build_file_tree(sample_project)
        assert ".git" not in tree


class TestFeatureExtraction:
    """Test feature extraction from README content."""

    def test_extracts_bullet_features(self, readme_gate):
        """Extracts feature names from bullet lists under a Features heading."""
        content = """# Title

## Features

- Fast CLI interface
- Automated testing
- Plugin system

## Tech Stack
"""
        features = readme_gate._extract_features(content)
        assert "Fast CLI interface" in features
        assert "Automated testing" in features

    def test_fallback_on_no_features(self, readme_gate):
        """Returns default when no features section found."""
        features = readme_gate._extract_features("# Title\n\nSome content")
        assert "developer tool" in features


class TestDBMethods:
    """Test readme_jobs DB methods."""

    def test_has_readme_false(self, in_memory_db):
        """has_readme returns False for unknown build."""
        assert not in_memory_db.has_readme("nonexistent-id")

    def test_has_readme_true_after_record(self, in_memory_db):
        """has_readme returns True after recording a completed job."""
        in_memory_db.record_readme_job(
            build_job_id="test-id",
            repo_url="https://github.com/test/test",
            status="completed",
        )
        assert in_memory_db.has_readme("test-id")

    def test_has_readme_false_for_failed(self, in_memory_db):
        """has_readme returns False for failed readme jobs."""
        in_memory_db.record_readme_job(
            build_job_id="test-id",
            repo_url="https://github.com/test/test",
            status="failed",
            error="some error",
        )
        assert not in_memory_db.has_readme("test-id")

    def test_get_readme_pending_with_published(self, in_memory_db):
        """get_readme_pending returns published builds without completed readme."""
        # Insert a publish job
        job = PublishJob(
            build_job_id="build-1",
            title="Test",
            repo_name="test",
            repo_url="https://github.com/test/test",
            status="published",
            project_dir="/tmp/test",
            published_at=datetime.now(),
        )
        in_memory_db.record_publish_job(job)

        pending = in_memory_db.get_readme_pending()
        assert len(pending) == 1
        assert pending[0]["build_job_id"] == "build-1"

    def test_get_readme_pending_excludes_completed(self, in_memory_db):
        """get_readme_pending excludes builds with completed readme."""
        job = PublishJob(
            build_job_id="build-1",
            title="Test",
            repo_name="test",
            repo_url="https://github.com/test/test",
            status="published",
            project_dir="/tmp/test",
            published_at=datetime.now(),
        )
        in_memory_db.record_publish_job(job)
        in_memory_db.record_readme_job(
            build_job_id="build-1",
            repo_url="https://github.com/test/test",
            status="completed",
        )

        pending = in_memory_db.get_readme_pending()
        assert len(pending) == 0


class TestProcessOneFailsGracefully:
    """Test that _process_one handles errors correctly."""

    def test_missing_project_dir(self, readme_gate, in_memory_db):
        """Returns failed status when project_dir doesn't exist."""
        result = readme_gate._process_one(
            build_job_id="test-id",
            title="Test",
            project_dir="/nonexistent/path",
            repo_url="https://github.com/test/test",
        )
        assert result["status"] == "failed"
        assert "not found" in result["error"]

    def test_no_llm_client(self, readme_gate, sample_project, in_memory_db):
        """Returns failed status when no LLM client is available."""
        readme_gate.client = None
        result = readme_gate._process_one(
            build_job_id="test-id",
            title="Test",
            project_dir=str(sample_project),
            repo_url="https://github.com/test/test",
        )
        assert result["status"] == "failed"
        assert "DEEPINFRA_API_KEY" in result["error"]


class TestCostCapture:
    """Test that LLM cost is recorded to the cost ledger after README generation."""

    def _mock_response(self, prompt_tokens: int = 100, completion_tokens: int = 50):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "# Title\n\nGenerated README content."
        mock_response.usage = MagicMock()
        mock_response.usage.prompt_tokens = prompt_tokens
        mock_response.usage.completion_tokens = completion_tokens
        return mock_response

    def test_records_cost_after_generation(self, readme_gate, in_memory_db):
        """After _generate_readme_content runs, a cost_ledger row is recorded."""
        # Use a model with known rates so estimated_cost > 0
        readme_gate.config.spec_llm_model = "sonnet"
        readme_gate.client = MagicMock()
        readme_gate.client.chat.completions.create.return_value = self._mock_response(100, 50)

        readme_gate._generate_readme_content(
            spec_text="spec", file_tree="tree", title="Test",
        )

        in_memory_db.connect()
        cur = in_memory_db.conn.cursor()
        cur.execute(
            "SELECT source, model, input_tokens, output_tokens, estimated_cost "
            "FROM cost_ledger ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None, "expected one cost ledger row after readme generation"
        source, model, in_toks, out_toks, est_cost = row
        assert source == "readme_generation"
        assert model == "sonnet"
        assert in_toks == 100
        assert out_toks == 50
        assert est_cost > 0

    def test_no_cost_recorded_when_state_db_none(self, readme_gate):
        """If state_db is None the call should still succeed (no exception)."""
        readme_gate.state_db = None
        readme_gate.client = MagicMock()
        readme_gate.client.chat.completions.create.return_value = self._mock_response()
        # Should not raise
        result = readme_gate._generate_readme_content(
            spec_text="spec", file_tree="tree", title="Test",
        )
        assert "# Title" in result

    def test_cost_recording_failure_does_not_break_gate(self, readme_gate, in_memory_db):
        """If record_cost raises, the gate still returns the README content."""
        readme_gate.config.spec_llm_model = "sonnet"
        readme_gate.client = MagicMock()
        readme_gate.client.chat.completions.create.return_value = self._mock_response()
        # Force record_cost to blow up
        readme_gate.state_db.record_cost = MagicMock(side_effect=RuntimeError("boom"))

        result = readme_gate._generate_readme_content(
            spec_text="spec", file_tree="tree", title="Test",
        )
        # Content still returned despite failed cost capture
        assert "# Title" in result


class TestFileTreeFiltering:
    """Test that internal pipeline artifacts are excluded from the file tree."""

    def test_excludes_self_healing_pipeline(self, readme_gate, sample_project):
        """Self-healing pipeline directory is excluded from the file tree."""
        (sample_project / ".self-healing-pipeline").mkdir()
        (sample_project / ".self-healing-pipeline" / "state.json").write_text("{}")
        tree = readme_gate._build_file_tree(sample_project)
        assert ".self-healing-pipeline" not in tree

    def test_excludes_heartbeat_callback(self, readme_gate, sample_project):
        """Heartbeat callback file is excluded from the file tree."""
        (sample_project / ".heartbeat-callback").write_text("")
        tree = readme_gate._build_file_tree(sample_project)
        assert ".heartbeat-callback" not in tree

    def test_excludes_gitignore_and_pytest_ini(self, readme_gate, sample_project):
        """Config noise files (.gitignore, pytest.ini) are excluded."""
        (sample_project / ".gitignore").write_text("*.pyc")
        (sample_project / "pytest.ini").write_text("[pytest]")
        tree = readme_gate._build_file_tree(sample_project)
        assert ".gitignore" not in tree
        assert "pytest.ini" not in tree

    def test_last_entry_connector_correct_after_filtering(self, readme_gate, tmp_path):
        """The └── connector is placed on the actual last visible entry,
        not on an excluded entry."""
        (tmp_path / ".git").mkdir()
        (tmp_path / "main.py").write_text("print('hi')")
        (tmp_path / ".gitignore").write_text("*.pyc")
        # .gitignore sorts after main.py but should be filtered
        tree = readme_gate._build_file_tree(tmp_path)
        # main.py should be the last (only) entry and use └──
        assert "└── main.py" in tree


class TestInfographicStripping:
    """Test that infographic references are stripped when image is missing."""

    def test_strips_html_img_tag(self, readme_gate):
        """HTML <img> tag pointing at infographic.png is removed."""
        content = (
            '<p align="center">\n'
            '  <img src="assets/infographic.png" alt="Test" width="800">\n'
            '</p>\n\n'
            '# Hello\n'
        )
        result = ReadmeGate._strip_infographic_refs(content, "Test")
        assert "infographic.png" not in result
        assert "# Hello" in result

    def test_strips_markdown_image(self, readme_gate):
        """Markdown image syntax is also stripped."""
        content = "![Test Overview](assets/infographic.png)\n\n# Hello"
        result = ReadmeGate._strip_infographic_refs(content, "Test")
        assert "infographic.png" not in result
        assert "# Hello" in result

    def test_removes_empty_p_wrapper(self, readme_gate):
        """Empty <p align="center"> wrapper is removed after img stripping."""
        content = (
            '<p align="center">\n'
            '  <img src="assets/infographic.png" alt="Test" width="800">\n'
            '</p>\n\n'
            '<h3 align="center">Tagline</h3>\n'
        )
        result = ReadmeGate._strip_infographic_refs(content, "Test")
        assert '<p align="center">' not in result
        assert "Tagline" in result


class TestHtmlFenceStripping:
    """Test that embedded ```html fences in LLM output are cleaned."""

    def test_strips_html_fence_at_start(self, readme_gate):
        """A ```html fence at the start of output is removed."""
        readme_gate.client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = (
            '```html\n'
            '<p align="center">\n'
            '  <img src="assets/infographic.png" alt="Test" width="800">\n'
            '</p>\n'
            '```\n\n'
            'It is 2 AM and your child is burning up.'
        )
        readme_gate.client.chat.completions.create.return_value = mock_response

        result = readme_gate._generate_readme_content("spec", "tree", "Test")
        assert not result.startswith("```html")
        assert '<p align="center">' in result


class TestCloneUrlInPrompt:
    """Test that clone_url is passed to the LLM prompt."""

    def test_clone_url_appears_in_prompt(self, readme_gate):
        """The clone URL is included in the user prompt."""
        readme_gate.client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "# Test\n\nContent"
        readme_gate.client.chat.completions.create.return_value = mock_response

        readme_gate._generate_readme_content(
            spec_text="spec",
            file_tree="tree",
            title="Test",
            clone_url="https://gitlab.com/m2ai-portfolio/wellness-copilot.git",
        )

        call_args = readme_gate.client.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "https://gitlab.com/m2ai-portfolio/wellness-copilot.git" in user_msg

    def test_has_infographic_in_prompt(self, readme_gate):
        """The has_infographic flag is included in the user prompt."""
        readme_gate.client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "# Test\n\nContent"
        readme_gate.client.chat.completions.create.return_value = mock_response

        readme_gate._generate_readme_content(
            spec_text="spec",
            file_tree="tree",
            title="Test",
            has_infographic=False,
        )

        call_args = readme_gate.client.chat.completions.create.call_args
        user_msg = call_args.kwargs["messages"][1]["content"]
        assert "no" in user_msg  # has_infographic="no"


# ---------------------------------------------------------------------------
# Tests for the 5 polish bugs found in the wellness-copilot run (2026-05-08).
# Bug 1: orphan `html` token left after fence strip
# Bug 2: <h3> dumps full description instead of compressed tagline
# Bug 3: stray closing ``` left after wrapped HTML banner block
# Bug 4: clone URL hallucinated to use queue_job_id
# Bug 5: file tree includes excluded entries (.self-healing-pipeline, etc.)
# ---------------------------------------------------------------------------


class TestCleanLLMOutput:
    """Bugs 1 and 3: cleanup chain handles real LLM output variance."""

    @pytest.mark.parametrize(
        "raw,expected_starts_with,must_not_contain",
        [
            # Bug 1 baseline: ```html opener with paired close
            (
                "```html\n<p align=\"center\">x</p>\n```\n\nIt is 2 AM",
                "<p align=",
                "html\n",  # orphan
            ),
            # Bug 3 variant: trailing space after closing fence
            (
                "```html\n<p>x</p>\n``` \n\nIt is 2 AM",
                "<p>x</p>",
                "```",
            ),
            # Bug 3 variant: tab after closing fence
            (
                "```html\n<p>x</p>\n```\t\n\nIt is 2 AM",
                "<p>x</p>",
                "```",
            ),
            # Bug 3 variant: no blank line between close and prose
            (
                "```html\n<p>x</p>\n```\nIt is 2 AM",
                "<p>x</p>",
                "```",
            ),
            # Bug 3 variant: closing fence with language tag echoed
            (
                "```html\n<p>x</p>\n```html\n\nIt is 2 AM",
                "<p>x</p>",
                "```",
            ),
            # Existing case: ```markdown opener
            (
                "```markdown\n# Title\n\nContent\n```",
                "# Title",
                "```",
            ),
        ],
    )
    def test_cleanup_handles_fence_variants(
        self, raw, expected_starts_with, must_not_contain
    ):
        """Cleanup handles common LLM fence-emission patterns."""
        result = ReadmeGate._clean_llm_output(raw)
        assert result.lstrip().startswith(expected_starts_with), (
            f"got: {result[:80]!r}"
        )
        assert must_not_contain not in result, (
            f"unexpectedly contains {must_not_contain!r}: {result[:200]!r}"
        )

    def test_cleanup_no_op_on_plain_markdown(self):
        """Plain markdown with no fences is returned unchanged (modulo trailing ws)."""
        raw = "# Title\n\nSome prose.\n"
        assert ReadmeGate._clean_llm_output(raw) == "# Title\n\nSome prose."

    def test_cleanup_handles_empty_input(self):
        assert ReadmeGate._clean_llm_output("") == ""

    def test_cleanup_strips_orphan_html_token(self):
        """If a previous step stripped ``` but left `html`, the orphan is removed."""
        raw = "html\n<p>banner</p>\n\nIt is 2 AM"
        result = ReadmeGate._clean_llm_output(raw)
        assert not result.startswith("html\n")
        assert result.startswith("<p>banner</p>")


class TestCompressTagline:
    """Bug 2: tagline compression."""

    def test_extracts_first_sentence(self):
        long = (
            "A personal health and wellness guidance agent. "
            "Built on Claude with persistent memory."
        )
        result = ReadmeGate._compress_tagline(long)
        assert result == "A personal health and wellness guidance agent."

    def test_caps_long_first_sentence_at_max_chars(self):
        # 200-char single sentence -> should be truncated
        long = ("A " + "very " * 60).strip() + " thing"  # no period
        result = ReadmeGate._compress_tagline(long, max_chars=100)
        assert len(result) <= 100

    def test_prefers_comma_break_when_truncating(self):
        long = (
            "A long tagline with " + "lots " * 10
            + ", and then a continuation that goes way past the cap"
        )
        result = ReadmeGate._compress_tagline(long, max_chars=80)
        assert len(result) <= 80
        # Should end with a clean break (period or ellipsis)
        assert result.endswith(".") or result.endswith("...")

    def test_empty_input_returns_empty(self):
        assert ReadmeGate._compress_tagline("") == ""
        assert ReadmeGate._compress_tagline(None or "") == ""

    def test_short_description_returned_as_is(self):
        short = "A simple CLI tool."
        assert ReadmeGate._compress_tagline(short) == short


class TestSlugFromRepoUrl:
    """Helper for bug 4 (and 5): slug derivation from repo URL."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://gitlab.com/m2ai-portfolio/wellness-copilot.git", "wellness-copilot"),
            ("https://github.com/foo/bar/", "bar"),
            ("https://github.com/foo/bar", "bar"),
            ("https://gitlab.com/group/sub/project.git", "project"),
            ("", ""),
        ],
    )
    def test_slug_extraction(self, url, expected):
        assert ReadmeGate._slug_from_repo_url(url) == expected


class TestNormalizeH3Tagline:
    """Bug 2 belt-and-suspenders: replace LLM-emitted h3 with canonical tagline."""

    def test_replaces_long_h3_inner_text(self):
        content = (
            '<p align="center"><img src="x.png"></p>\n'
            '<h3 align="center">A very long description that the LLM dumped '
            'verbatim instead of compressing it. Goes on and on.</h3>\n\n'
            "Body text"
        )
        canonical = "A short tagline."
        result = ReadmeGate._normalize_h3_tagline(content, canonical)
        assert '<h3 align="center">A short tagline.</h3>' in result
        assert "Goes on and on" not in result

    def test_no_op_with_empty_tagline(self):
        content = '<h3 align="center">Original</h3>'
        assert ReadmeGate._normalize_h3_tagline(content, "") == content

    def test_no_op_when_no_h3(self):
        content = "# Just markdown\n\nNo h3 tag here."
        assert ReadmeGate._normalize_h3_tagline(content, "tagline") == content

    def test_handles_single_quotes(self):
        content = "<h3 align='center'>Old</h3>"
        result = ReadmeGate._normalize_h3_tagline(content, "New")
        assert "New" in result


class TestNormalizeCloneUrl:
    """Bug 4: replace LLM-hallucinated clone URL with the canonical one."""

    def test_replaces_first_clone_url(self):
        content = (
            "1. Clone the repository:\n"
            "   ```bash\n"
            "   git clone https://gitlab.com/m2ai-portfolio/metroplex-ideaforge-414.git\n"
            "   ```\n"
        )
        canonical = "https://gitlab.com/m2ai-portfolio/wellness-copilot.git"
        result = ReadmeGate._normalize_clone_url(content, canonical)
        assert canonical in result
        assert "metroplex-ideaforge-414" not in result

    def test_no_op_with_empty_canonical(self):
        content = "git clone https://example.com/x.git"
        assert ReadmeGate._normalize_clone_url(content, "") == content

    def test_no_op_when_no_clone_command(self):
        content = "Just some text without a clone command."
        assert ReadmeGate._normalize_clone_url(content, "https://x") == content

    def test_only_replaces_first_occurrence(self):
        content = (
            "git clone https://wrong.com/a.git\n"
            "git clone https://wrong.com/b.git\n"
        )
        canonical = "https://right.com/c.git"
        result = ReadmeGate._normalize_clone_url(content, canonical)
        # Only first occurrence is rewritten
        assert result.count(canonical) == 1
        assert "https://wrong.com/b.git" in result


class TestReplaceFileTreeBlock:
    """Bug 5: replace LLM-hallucinated file tree with the canonical one."""

    def test_replaces_tree_block(self):
        content = (
            "### How It Works\n\n"
            "```\n"
            "wellness-copilot/\n"
            "├── .self-healing-pipeline/\n"
            "├── .heartbeat-callback\n"
            "└── main.py\n"
            "```\n\n"
            "More text."
        )
        canonical = (
            "wellness-copilot/\n"
            "├── tests/\n"
            "└── main.py"
        )
        result = ReadmeGate._replace_file_tree_block(content, canonical)
        assert ".self-healing-pipeline" not in result
        assert ".heartbeat-callback" not in result
        assert "├── tests/" in result

    def test_skips_non_tree_fenced_blocks(self):
        """A fenced block without tree characters is left alone."""
        content = (
            "```bash\n"
            "git clone https://example.com\n"
            "```\n\n"
            "```\n"
            "wellness-copilot/\n"
            "├── tests/\n"
            "└── main.py\n"
            "```\n"
        )
        canonical = "wellness-copilot/\n└── new.py"
        result = ReadmeGate._replace_file_tree_block(content, canonical)
        # Bash block preserved
        assert "git clone https://example.com" in result
        # Tree block replaced
        assert "├── tests/" not in result
        assert "└── new.py" in result

    def test_no_op_when_no_tree_block(self):
        content = "# README\n\n```bash\nnpm install\n```\n"
        canonical = "x/\n└── y.py"
        result = ReadmeGate._replace_file_tree_block(content, canonical)
        assert result == content

    def test_no_op_with_empty_tree(self):
        content = "```\nfoo/\n├── bar\n```\n"
        assert ReadmeGate._replace_file_tree_block(content, "") == content


class TestBuildFileTreeRootName:
    """Bug 4 root cause: root_name override prevents queue_job_id leakage."""

    def test_root_name_override(self, readme_gate, sample_project):
        tree = readme_gate._build_file_tree(sample_project, root_name="my-project")
        first_line = tree.split("\n", 1)[0]
        assert first_line == "my-project/"

    def test_default_root_is_dir_basename(self, readme_gate, sample_project):
        tree = readme_gate._build_file_tree(sample_project)
        first_line = tree.split("\n", 1)[0]
        assert first_line == sample_project.name + "/"


class TestWellnessCopilotRegression:
    """Regression: full cleanup chain on the actual 2026-05-08 wellness-copilot
    output should fix all 5 bugs."""

    PRODUCTION_README = (
        "```html\n"
        '<p align="center">\n'
        '  <img src="assets/infographic.png" alt="Wellness Copilot" width="800">\n'
        "</p>\n\n"
        '<h3 align="center">A personal health and wellness guidance agent that '
        "provides symptom interpretation, nutrition planning, medication "
        "interaction awareness, mental health check-ins, and preventive care "
        "reminders. Built on Claude with persistent memory so it learns your "
        "health context over time -- family history, allergies, fitness goals, "
        "sleep patterns.</h3>\n\n"
        '<p align="center">\n'
        '  <a href="#the-turn">How it helps</a> &bull;\n'
        '  <a href="#quick-start">Quick Start</a> &bull;\n'
        '  <a href="#how-it-works">How It Works</a>\n'
        "</p>\n"
        "```\n\n"
        "It is 2 AM, and your child is burning up with a fever.\n\n"
        "### Quick Start\n\n"
        "1. Clone:\n"
        "   ```bash\n"
        "   git clone https://gitlab.com/m2ai-portfolio/metroplex-ideaforge-414.git\n"
        "   ```\n\n"
        "### How It Works\n\n"
        "```\n"
        "wellness-copilot/\n"
        "├── .self-healing-pipeline/  # Pipeline configuration and logs\n"
        "├── tests/                  # Test files\n"
        "├── .heartbeat-callback     # Heartbeat callback configuration\n"
        "└── wellness.py             # Main CLI entrypoint\n"
        "```\n"
    )

    def test_full_cleanup_pipeline_fixes_all_bugs(self):
        canonical_url = "https://gitlab.com/m2ai-portfolio/wellness-copilot.git"
        canonical_tagline = "A personal health and wellness guidance agent."
        canonical_tree = (
            "wellness-copilot/\n"
            "├── tests/\n"
            "└── wellness.py"
        )

        # Apply the same chain _process_one runs
        content = ReadmeGate._clean_llm_output(self.PRODUCTION_README)
        content = ReadmeGate._normalize_h3_tagline(content, canonical_tagline)
        content = ReadmeGate._normalize_clone_url(content, canonical_url)
        content = ReadmeGate._replace_file_tree_block(content, canonical_tree)

        # Bug 1: no orphan html token
        assert not content.startswith("html"), f"line 1: {content[:30]!r}"

        # Bug 2: h3 is the compressed tagline, not the full description
        assert f'<h3 align="center">{canonical_tagline}</h3>' in content
        assert "family history, allergies" not in content

        # Bug 3: no stray closing ``` after the banner
        # (Allowed: the bash code block fences for git clone)
        first_50_lines = "\n".join(content.split("\n")[:8])
        assert "```" not in first_50_lines, (
            f"banner area still has ``` :\n{first_50_lines}"
        )

        # Bug 4: clone URL is canonical, not queue_job_id
        assert "metroplex-ideaforge-414" not in content
        assert canonical_url in content

        # Bug 5: tree excludes pipeline-internal entries
        assert ".self-healing-pipeline" not in content
        assert ".heartbeat-callback" not in content
        assert "└── wellness.py" in content
