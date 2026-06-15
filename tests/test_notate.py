"""
Unit tests for the pure (no git / no network) parts of notate.

Run with the standard library, no extra dependencies:

    python -m unittest discover tests
"""
import os
import sys
import unittest

# Make `import notate` work and let the module load without an ~/.env.notate file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in ("ANTHROPIC_API_KEY", "NOTION_API_KEY", "NOTION_DOCS_DATABASE_ID"):
    os.environ.setdefault(_k, "test")

import notate  # noqa: E402


class DetectDocType(unittest.TestCase):
    def test_forced_type_wins(self):
        self.assertEqual(notate.detect_doc_type("anything", "api"), "api")

    def test_backend_only(self):
        diff = "+++ b/app/api/users_controller.py\n+def index(): ..."
        self.assertEqual(notate.detect_doc_type(diff, None), "api")

    def test_frontend_only(self):
        diff = "+++ b/src/components/Button.tsx\n+export const Button = () => null"
        self.assertEqual(notate.detect_doc_type(diff, None), "frontend")

    def test_mixed(self):
        diff = ("+++ b/app/api/route.py\n+def handler(): ...\n"
                "+++ b/src/pages/Home.tsx\n+export default Home")
        self.assertEqual(notate.detect_doc_type(diff, None), "mixed")

    def test_default_is_mixed(self):
        self.assertEqual(notate.detect_doc_type("+++ b/README.md", None), "mixed")


class NormalizeLanguage(unittest.TestCase):
    def test_known_alias(self):
        self.assertEqual(notate.normalize_language("tsx"), "typescript")
        self.assertEqual(notate.normalize_language("PY"), "python")

    def test_unknown_passthrough(self):
        self.assertEqual(notate.normalize_language("go"), "go")

    def test_empty_is_plain_text(self):
        self.assertEqual(notate.normalize_language(""), "plain text")


class ParseInline(unittest.TestCase):
    def test_plain_text(self):
        out = notate.parse_inline("hello world")
        self.assertEqual(out, [{"type": "text", "text": {"content": "hello world"}}])

    def test_bold(self):
        out = notate.parse_inline("a **b** c")
        bold = [p for p in out if p.get("annotations", {}).get("bold")]
        self.assertEqual(len(bold), 1)
        self.assertEqual(bold[0]["text"]["content"], "b")

    def test_inline_code(self):
        out = notate.parse_inline("call `fn()` now")
        code = [p for p in out if p.get("annotations", {}).get("code")]
        self.assertEqual(code[0]["text"]["content"], "fn()")


class MdToNotionBlocks(unittest.TestCase):
    def test_headings(self):
        blocks = notate.md_to_notion_blocks("# H1\n## H2\n### H3")
        self.assertEqual([b["type"] for b in blocks],
                         ["heading_1", "heading_2", "heading_3"])

    def test_bullets_and_numbers(self):
        blocks = notate.md_to_notion_blocks("- one\n1. two")
        self.assertEqual([b["type"] for b in blocks],
                         ["bulleted_list_item", "numbered_list_item"])

    def test_code_block(self):
        blocks = notate.md_to_notion_blocks("```python\nprint(1)\n```")
        self.assertEqual(blocks[0]["type"], "code")
        self.assertEqual(blocks[0]["code"]["language"], "python")
        self.assertIn("print(1)", blocks[0]["code"]["rich_text"][0]["text"]["content"])

    def test_table(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |"
        blocks = notate.md_to_notion_blocks(md)
        self.assertEqual(blocks[0]["type"], "table")
        self.assertEqual(blocks[0]["table"]["table_width"], 2)
        # header row + one data row
        self.assertEqual(len(blocks[0]["table"]["children"]), 2)

    def test_empty(self):
        self.assertEqual(notate.md_to_notion_blocks("   "), [])


class BuildPrompt(unittest.TestCase):
    def _commit(self, body=""):
        return {"date": "2026-06-15", "sha": "abc1234",
                "message": "fix thing", "body": body, "author": "Mo"}

    def test_branch_included(self):
        p = notate.build_prompt("DIFF", [self._commit()], "api",
                                branch="NIXON-305-fix-export")
        self.assertIn("NIXON-305-fix-export", p)

    def test_branch_absent(self):
        p = notate.build_prompt("DIFF", [self._commit()], "api", branch=None)
        self.assertNotIn("BRANCH NAME", p)

    def test_commit_body_included(self):
        p = notate.build_prompt("DIFF", [self._commit(body="Why: prevents X")],
                                "api")
        self.assertIn("Why: prevents X", p)

    def test_type_instructions(self):
        p = notate.build_prompt("DIFF", [self._commit()], "api")
        self.assertIn("HTTP method and route", p)
        self.assertIn("DIFF", p)


class DetectAreas(unittest.TestCase):
    def test_frontend(self):
        self.assertEqual(notate.detect_areas(["src/components/Button.tsx"]), ["frontend"])

    def test_backend(self):
        self.assertEqual(notate.detect_areas(["backend/server/app/main.py"]), ["backend"])

    def test_multi_area_ordered(self):
        files = ["src/pages/Home.tsx", "backend/api/route.py", "db/migrations/001.sql"]
        self.assertEqual(notate.detect_areas(files), ["backend", "frontend", "db"])

    def test_test_file_tagged(self):
        areas = notate.detect_areas(["backend/tests/test_x.py"])
        self.assertIn("tests", areas)
        self.assertIn("backend", areas)

    def test_infra_and_ci(self):
        self.assertEqual(notate.detect_areas(["k8s/base/deploy.yaml", ".github/workflows/ci.yml"]),
                         ["infra", "ci"])

    def test_docs(self):
        self.assertEqual(notate.detect_areas(["README.md"]), ["docs"])

    def test_fallback_other(self):
        self.assertEqual(notate.detect_areas(["weird/config.toml"]), ["other"])

    def test_empty(self):
        self.assertEqual(notate.detect_areas([]), [])


class Traceability(unittest.TestCase):
    def test_parse_slug_ssh(self):
        self.assertEqual(notate.parse_repo_slug("git@github.com:owner/repo.git"), "owner/repo")

    def test_parse_slug_https(self):
        self.assertEqual(notate.parse_repo_slug("https://github.com/owner/repo.git"), "owner/repo")

    def test_parse_slug_host_alias(self):
        self.assertEqual(notate.parse_repo_slug("git@github.com-segunda:mogamiGit/notate.git"),
                         "mogamiGit/notate")

    def test_pr_number_from_merge(self):
        ci = [{"message": "Merge pull request #226 from org/NIXON-305-fix"}]
        self.assertEqual(notate.extract_pr_number(ci), "226")

    def test_pr_number_from_squash(self):
        self.assertEqual(notate.extract_pr_number([{"message": "feat: thing (#42)"}]), "42")

    def test_pr_number_none(self):
        self.assertIsNone(notate.extract_pr_number([{"message": "plain commit"}]))

    def test_ticket_from_branch(self):
        self.assertEqual(notate.extract_ticket("NIXON-305-fix-export", []), "NIXON-305")

    def test_ticket_from_merge_message(self):
        ci = [{"message": "Merge pull request #226 from org/NIXON-305-fix", "body": ""}]
        self.assertEqual(notate.extract_ticket(None, ci), "NIXON-305")

    def test_ticket_none(self):
        self.assertIsNone(notate.extract_ticket(None, [{"message": "no ticket", "body": ""}]))

    def test_build_pr_url_pr(self):
        self.assertEqual(notate.build_pr_url("owner/repo", "226", []),
                         "https://github.com/owner/repo/pull/226")

    def test_build_pr_url_commit_fallback(self):
        ci = [{"sha": "abc1234"}]
        self.assertEqual(notate.build_pr_url("owner/repo", None, ci),
                         "https://github.com/owner/repo/commit/abc1234")

    def test_build_pr_url_no_slug(self):
        self.assertIsNone(notate.build_pr_url(None, "226", []))


class DocSchema(unittest.TestCase):
    def test_required_matches_properties(self):
        props = set(notate.DOC_SCHEMA["properties"])
        required = set(notate.DOC_SCHEMA["required"])
        self.assertEqual(props, required)

    def test_no_additional_properties(self):
        self.assertIs(notate.DOC_SCHEMA["additionalProperties"], False)


if __name__ == "__main__":
    unittest.main()
