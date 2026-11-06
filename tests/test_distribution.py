"""Verify portable packaging, isolated installation, and overwrite protection."""

import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from install_skill import default_destination, install_skill
from package_skill import package_skill, runtime_files, validate_skill


class TestDistribution(unittest.TestCase):
    def test_archive_and_install_have_same_portable_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            archive_path = temp / "artifact.zip"
            count, digest = package_skill(archive_path)
            installed, copied = install_skill(temp / "skills")
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertEqual(count, copied)
                self.assertEqual(count, len(names))
                self.assertIn("data-detective/SKILL.md", names)
                self.assertIn("data-detective/LICENSE", names)
                self.assertIn("data-detective/scripts/detective.py", names)
                self.assertIn("data-detective/scripts/requirements.txt", names)
                self.assertIn("data-detective/schemas/criminal-analysis.schema.json", names)
                for name in names:
                    relative = Path(name).relative_to("data-detective")
                    self.assertEqual(archive.read(name), (installed / relative).read_bytes())
                    self.assertNotIn("..", relative.parts)
                    self.assertNotIn("__pycache__", relative.parts)
                    self.assertNotIn("outputs", relative.parts)
                    self.assertNotIn("tests", relative.parts)
                    self.assertNotIn("test", relative.parts)
            self.assertEqual(64, len(digest))
            other_count, other_digest = package_skill(temp / "second.zip")
            self.assertEqual((count, digest), (other_count, other_digest))
            self.assertEqual(archive_path.read_bytes(), (temp / "second.zip").read_bytes())

    def test_packaging_cli_writes_a_matching_checksum_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "candidate.zip"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/package_skill.py"), "--out", str(archive)],
                capture_output=True, text=True,
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(f"{digest}  candidate.zip\n",
                             archive.with_name("candidate.zip.sha256").read_text(encoding="utf-8"))

    def test_packaging_cli_fails_if_checksum_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "candidate.zip"
            archive.with_name("candidate.zip.sha256").mkdir()
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts/package_skill.py"), "--out", str(archive)],
                capture_output=True, text=True,
            )
            self.assertEqual(1, completed.returncode)
            self.assertIn("Error:", completed.stderr)
            self.assertNotIn("Packaged", completed.stdout)

    def test_existing_installation_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            installed, _ = install_skill(parent)
            marker = installed / "my-local-note.txt"
            marker.write_text("keep this", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "already exists"):
                install_skill(parent)
            self.assertEqual("keep this", marker.read_text(encoding="utf-8"))

    def test_installed_cli_runs_outside_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            installed, _ = install_skill(parent / "skills")
            source = parent / "records.csv"
            source.write_text("name,identifier\nAlice,A001\nBob,B002\n", encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(installed / "scripts/detective.py"), "investigate",
                 "--mode", "entity", "--target", "Alice", "--sources", str(source),
                 "--out", str(parent / "report.json"), "--no-html"],
                cwd=parent, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            report = (parent / "report.json").read_text(encoding="utf-8")
            self.assertIn("Alice", report)
            self.assertIn("A001", report)

    def test_cli_requires_an_explicit_destination(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts/install_skill.py")],
            capture_output=True, text=True,
        )
        self.assertEqual(2, completed.returncode)
        self.assertIn("--target", completed.stderr)

    def test_explicit_host_destinations(self):
        self.assertEqual(Path.home() / ".agents" / "skills", default_destination("codex"))
        self.assertEqual(Path.home() / ".claude" / "skills", default_destination("claude"))

    def test_distribution_metadata_links_and_schemas_validate(self):
        self.assertGreater(validate_skill(), 0)

    def test_broken_runtime_link_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text(
                "---\nname: data-detective\ndescription: Data analysis\n---\n[Missing](references/missing.md)\n",
                encoding="utf-8",
            )
            (root / "LICENSE").write_text("Test fixture license", encoding="utf-8")
            for directory in ("scripts", "references", "schemas"):
                (root / directory).mkdir()
            with self.assertRaisesRegex(ValueError, "outside the runtime package"):
                validate_skill(root)

    def test_secrets_and_local_artifacts_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text("---\nname: data-detective\n---\n", encoding="utf-8")
            for directory in ("scripts", "references", "schemas", "agents", "outputs", "test"):
                (root / directory).mkdir()
            for relative in ("scripts/valid.py", "scripts/requirements.txt", "references/guide.md",
                             "schemas/report.json", "agents/openai.yaml", "outputs/private.json",
                             "test/private.md", "scripts/.env", "scripts/credentials.json",
                             "references/.env.md", "scripts/__pycache__/valid.pyc"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("sample", encoding="utf-8")
            self.assertEqual({"SKILL.md", "scripts/valid.py", "scripts/requirements.txt",
                              "references/guide.md", "schemas/report.json", "agents/openai.yaml"},
                             {path.as_posix() for path in runtime_files(root)})

    def test_linked_runtime_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "SKILL.md").write_text("---\nname: data-detective\n---\n", encoding="utf-8")
            for directory in ("scripts", "references", "schemas"):
                (root / directory).mkdir()
            original = root / "outside.py"
            original.write_text("private = True", encoding="utf-8")
            try:
                (root / "scripts/linked.py").symlink_to(original)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "regular file"):
                runtime_files(root)

    def test_interrupted_copy_removes_partial_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            with mock.patch("install_skill.shutil.copyfile", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    install_skill(parent)
            self.assertFalse((parent / "data-detective").exists())


if __name__ == "__main__":
    unittest.main()
