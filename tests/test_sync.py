import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import olsync as s
import web_transport as w


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name) / "local"
        self.remote = Path(self.temp.name) / "remote"
        self.remote.mkdir()
        s.initialize(self.local, str(self.remote))
        self.sync = s.Synchronizer(self.local)

    def write(self, root, path, value):
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(value.encode() if isinstance(value, str) else value)

    def seed(self):
        self.write(self.local, "main.tex", "one\ntwo\nthree\n")
        self.sync.sync()

    def test_initial_union_and_nested_binary(self):
        self.write(self.local, "figures/汉 字.png", b"\0image\xff")
        self.write(self.remote, "main.tex", "hello")
        self.sync.sync()
        self.assertEqual((self.remote / "figures/汉 字.png").read_bytes(), b"\0image\xff")
        self.assertEqual((self.local / "main.tex").read_text(), "hello")

    def test_push_then_pull(self):
        self.seed()
        self.write(self.local, "main.tex", "local edit")
        self.sync.sync()
        self.assertEqual((self.remote / "main.tex").read_text(), "local edit")
        self.write(self.remote, "main.tex", "remote edit")
        self.sync.sync()
        self.assertEqual((self.local / "main.tex").read_text(), "remote edit")

    def test_different_files_both_directions(self):
        self.seed()
        self.write(self.local, "main.tex", "local edit")
        self.write(self.remote, "refs.bib", "remote new")
        self.sync.sync()
        self.assertEqual((self.remote / "main.tex").read_text(), "local edit")
        self.assertEqual((self.local / "refs.bib").read_text(), "remote new")

    def test_nonoverlapping_changes_merge_both_sides_and_keep_backup(self):
        self.seed()
        self.write(self.local, "main.tex", "LOCAL\ntwo\nthree\n")
        self.write(self.remote, "main.tex", "one\ntwo\nREMOTE\n")
        report = self.sync.sync()
        self.assertEqual(report["auto_merged"], ["main.tex"])
        for root in (self.local, self.remote):
            self.assertEqual((root / "main.tex").read_text(), "LOCAL\ntwo\nREMOTE\n")
        archive = Path(report["merge_backup"])
        self.assertEqual((archive / "base/main.tex").read_text(), "one\ntwo\nthree\n")
        self.assertEqual((archive / "local/main.tex").read_text(), "LOCAL\ntwo\nthree\n")
        self.assertEqual((archive / "overleaf/main.tex").read_text(), "one\ntwo\nREMOTE\n")
        self.assertEqual(self.sync.sync()["actions"], [])

    def test_merge_preview_changes_no_files_and_creates_no_backup(self):
        self.seed()
        before = (self.sync.state / "baseline.json").read_bytes()
        self.write(self.local, "main.tex", "LOCAL\ntwo\nthree\n")
        self.write(self.remote, "main.tex", "one\ntwo\nREMOTE\n")
        report = self.sync.sync(dry_run=True)
        self.assertEqual(report["auto_merged"], ["main.tex"])
        self.assertFalse((self.sync.state / "merge-backups").exists())
        self.assertFalse((self.sync.state / "pending.json").exists())
        self.assertEqual((self.sync.state / "baseline.json").read_bytes(), before)
        self.assertEqual((self.local / "main.tex").read_text(), "LOCAL\ntwo\nthree\n")
        self.assertEqual((self.remote / "main.tex").read_text(), "one\ntwo\nREMOTE\n")

    def test_missing_or_damaged_base_falls_back_to_conflict(self):
        self.seed()
        obj = self.sync.state / "objects" / self.sync.baseline()["main.tex"]
        self.write(self.local, "main.tex", "LOCAL\ntwo\nthree\n")
        self.write(self.remote, "main.tex", "one\ntwo\nREMOTE\n")
        for content in (None, b"corrupt"):
            if content is None:
                obj.unlink()
            else:
                obj.write_bytes(content)
            with self.assertRaises(s.ConflictError):
                self.sync.sync()
            self.assertFalse((self.sync.state / "pending.json").exists())

    def test_explicit_choice_overrides_automatic_merge(self):
        self.seed()
        self.write(self.local, "main.tex", "LOCAL\ntwo\nthree\n")
        self.write(self.remote, "main.tex", "one\ntwo\nREMOTE\n")
        report = self.sync.sync(take_local=["main.tex"])
        self.assertEqual(report["auto_merged"], [])
        self.assertEqual((self.remote / "main.tex").read_text(), "LOCAL\ntwo\nthree\n")

    def test_conflict_in_another_file_blocks_merge_before_any_write(self):
        self.seed()
        self.write(self.local, "main.tex", "LOCAL\ntwo\nthree\n")
        self.write(self.remote, "main.tex", "one\ntwo\nREMOTE\n")
        self.write(self.local, "other.tex", "left")
        self.write(self.remote, "other.tex", "right")
        with self.assertRaises(s.ConflictError):
            self.sync.sync()
        self.assertEqual((self.remote / "main.tex").read_text(), "one\ntwo\nREMOTE\n")
        self.assertFalse((self.sync.state / "merge-backups").exists())

    def test_interrupted_merge_can_recover_exact_combined_content(self):
        self.seed()
        self.write(self.local, "main.tex", "LOCAL\ntwo\nthree\n")
        self.write(self.remote, "main.tex", "one\ntwo\nREMOTE\n")
        with patch.object(self.sync, "apply_local", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.sync.sync()
        self.assertEqual((self.remote / "main.tex").read_text(), "LOCAL\ntwo\nREMOTE\n")
        self.sync.recover()
        self.assertEqual((self.local / "main.tex").read_text(), "LOCAL\ntwo\nREMOTE\n")
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_local_edit_during_merge_backup_is_not_overwritten(self):
        self.seed()
        self.write(self.local, "main.tex", "LOCAL\ntwo\nthree\n")
        self.write(self.remote, "main.tex", "one\ntwo\nREMOTE\n")
        original = self.sync.archive_merge

        def edited(*args):
            result = original(*args)
            self.write(self.local, "main.tex", "editing during backup")
            return result
        with patch.object(self.sync, "archive_merge", edited), self.assertRaises(s.SyncError):
            self.sync.sync()
        self.assertEqual((self.local / "main.tex").read_text(), "editing during backup")
        self.assertEqual((self.remote / "main.tex").read_text(), "one\ntwo\nREMOTE\n")
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_long_line_conflict_excerpt_shows_changed_text(self):
        prefix = "identical prefix " * 40
        self.write(self.local, "main.tex", prefix + "LOCAL CHOICE" + " suffix" * 40)
        self.write(self.remote, "main.tex", prefix + "REMOTE CHOICE" + " suffix" * 40)
        with self.assertRaises(s.ConflictError) as caught:
            self.sync.sync()
        detail = caught.exception.report["conflicts"][0]
        self.assertIn("LOCAL CHOICE", "\n".join(detail["diff"]))
        self.assertIn("REMOTE CHOICE", "\n".join(detail["diff"]))
        self.assertTrue(detail["truncated"])
        self.assertTrue(all(len(line) <= 240 for line in detail["diff"]))

    def test_conflict_aborts_entire_plan_and_lists_lines(self):
        self.seed()
        self.write(self.local, "main.tex", "one\nLOCAL\nthree\n")
        self.write(self.remote, "main.tex", "one\nREMOTE\nthree\n")
        self.write(self.local, "new.tex", "do not upload")
        self.write(self.remote, "remote.tex", "do not download")
        with self.assertRaises(s.ConflictError) as caught:
            self.sync.sync()
        diff = "\n".join(caught.exception.report["conflicts"][0]["diff"])
        self.assertIn("@@", diff)
        self.assertIn("-LOCAL", diff)
        self.assertIn("+REMOTE", diff)
        self.assertFalse((self.remote / "new.tex").exists())
        self.assertFalse((self.local / "remote.tex").exists())
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_first_sync_differences_are_not_guessed_by_time(self):
        self.write(self.local, "main.tex", "local")
        self.write(self.remote, "main.tex", "remote")
        os.utime(self.local / "main.tex", (2_000_000_000, 2_000_000_000))
        with self.assertRaises(s.ConflictError):
            self.sync.sync()

    def test_explicit_resolution(self):
        self.write(self.local, "main.tex", "local")
        self.write(self.remote, "main.tex", "remote")
        self.sync.sync(take_remote=["main.tex"])
        self.assertEqual((self.local / "main.tex").read_text(), "remote")
        self.write(self.local, "main.tex", "mine")
        self.write(self.remote, "main.tex", "theirs")
        self.sync.sync(take_local=["main.tex"])
        self.assertEqual((self.remote / "main.tex").read_text(), "mine")

    def test_dry_run_does_not_advance_baseline(self):
        self.write(self.local, "main.tex", "local")
        report = self.sync.sync(dry_run=True)
        self.assertEqual(report["actions"][0]["side"], "overleaf")
        self.assertFalse((self.remote / "main.tex").exists())
        self.assertFalse((self.sync.state / "baseline.json").exists())

    def test_identical_simultaneous_edits(self):
        self.seed()
        for root in (self.local, self.remote):
            self.write(root, "main.tex", "same new contents")
        self.assertEqual(self.sync.sync()["actions"], [])

    def test_touch_does_not_upload(self):
        self.seed()
        os.utime(self.local / "main.tex", None)
        self.assertEqual(self.sync.sync()["actions"], [])

    def test_crlf_text_compares_equal_without_rewriting_local(self):
        self.write(self.local, 'main.tex', b'hello\r\nworld\r\n')
        self.write(self.remote, 'main.tex', b'hello\nworld\n')
        self.assertEqual(self.sync.sync()['actions'], [])
        self.assertEqual((self.local / 'main.tex').read_bytes(), b'hello\r\nworld\r\n')
        self.write(self.remote, 'main.tex', b'updated\n')
        self.sync.sync()
        self.assertEqual((self.local / 'main.tex').read_bytes(), b'updated\n')

    def test_same_mtime_different_content_detected(self):
        self.seed()
        ns = (self.local / 'main.tex').stat().st_mtime_ns
        self.write(self.local, 'main.tex', 'different contents')
        os.utime(self.local / 'main.tex', ns=(ns, ns))
        self.sync.sync()
        self.assertEqual((self.remote / 'main.tex').read_text(), 'different contents')

    def test_preserve_download_timestamp(self):
        self.write(self.remote, "file.tex", "remote")
        ns = 1_700_000_000_123_456_789
        os.utime(self.remote / "file.tex", ns=(ns, ns))
        self.sync.sync()
        self.assertEqual((self.local / "file.tex").stat().st_mtime_ns, ns)

    def test_deletion_both_directions(self):
        self.seed()
        (self.local / "main.tex").unlink()
        self.sync.sync()
        self.assertFalse((self.remote / "main.tex").exists())
        self.write(self.remote, "another.tex", "remote")
        self.sync.sync()
        (self.remote / "another.tex").unlink()
        self.sync.sync()
        self.assertFalse((self.local / "another.tex").exists())

    def test_delete_vs_edit_conflict(self):
        self.seed()
        (self.local / "main.tex").unlink()
        self.write(self.remote, "main.tex", "changed")
        with self.assertRaises(s.ConflictError):
            self.sync.sync()
        self.assertEqual((self.remote / "main.tex").read_text(), "changed")

    def test_binary_conflict_is_summarized(self):
        for root, data in ((self.local, b"\0one"), (self.remote, b"\0two")):
            self.write(root, "fig.png", data)
        with self.assertRaises(s.ConflictError) as caught:
            self.sync.sync()
        self.assertTrue(caught.exception.report["conflicts"][0]["binary"])

    def test_excludes_build_but_keeps_pdf_figures(self):
        for name in ["main.pdf", "main.aux", "backups/old.tex", ".env", ".git/config", "output/x.pdf"]:
            self.write(self.local, name, "private/generated")
        self.write(self.local, "figures/figure.pdf", b"%PDF figure")
        self.sync.sync()
        self.assertEqual(set(s.local_snapshot(self.remote, [])), {"figures/figure.pdf"})

    def test_local_symlink_is_rejected(self):
        external = Path(self.temp.name) / "secret"
        external.write_text("secret")
        (self.local / "link.tex").symlink_to(external)
        with self.assertRaises(s.SyncError):
            self.sync.sync()
        self.assertFalse((self.remote / "link.tex").exists())

    def test_file_directory_collision(self):
        self.write(self.local, "figures", "file")
        self.write(self.remote, "figures/x.png", "nested")
        with self.assertRaises(s.ConflictError):
            self.sync.sync()

    def test_existing_ignored_directory_collision(self):
        self.write(self.local, "foo/.ignored", "keep")
        self.write(self.remote, "foo", "file")
        with self.assertRaises(s.SyncError):
            self.sync.sync()
        self.assertTrue((self.local / "foo/.ignored").exists())

    def test_concurrent_sync_lock(self):
        with self.sync.lock(), self.assertRaises(s.SyncError):
            self.sync.sync()

    def test_recover_after_remote_accepted_before_local_apply(self):
        self.seed()
        self.write(self.local, "main.tex", "new")
        self.write(self.remote, "refs.bib", "reference")
        with patch.object(self.sync, "apply_local", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                self.sync.sync()
        self.assertTrue((self.sync.state / "pending.json").exists())
        self.assertEqual((self.remote / "main.tex").read_text(), "new")
        self.sync.recover()
        self.assertEqual((self.local / "refs.bib").read_text(), "reference")
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_recover_partial_remote_upload(self):
        self.seed()
        self.write(self.local, "a.tex", "a")
        self.write(self.local, "b.tex", "b")

        def partial(transport, expected, desired):
            self.write(self.remote, "a.tex", desired["a.tex"].data)
            raise s.SyncError("network lost")
        with patch.object(s.DirectoryTransport, "apply", partial), self.assertRaises(s.SyncError):
            self.sync.sync()
        self.sync.recover()
        self.assertEqual((self.remote / "b.tex").read_text(), "b")

    def test_recover_does_not_overwrite_new_local_edits(self):
        self.seed()
        self.write(self.remote, "main.tex", "remote")
        with patch.object(self.sync, "apply_local", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.sync.sync()
        self.write(self.local, "main.tex", "new unsynced edits")
        with self.assertRaises(s.SyncError):
            self.sync.recover()
        self.assertEqual((self.local / "main.tex").read_text(), "new unsynced edits")

    def test_recover_does_not_overwrite_new_remote_edits(self):
        self.seed()
        self.write(self.local, "main.tex", "local")
        with patch.object(self.sync, "apply_local", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.sync.sync()
        self.write(self.remote, "main.tex", "new remote edits")
        with self.assertRaises(s.SyncError):
            self.sync.recover()
        self.assertEqual((self.remote / "main.tex").read_text(), "new remote edits")

    def interrupted_pull(self):
        self.seed()
        self.write(self.remote, "main.tex", "remote v1")
        with patch.object(self.sync, "apply_local", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.sync.sync()

    def test_pull_commits_snapshot_without_reexport_or_remote_write(self):
        self.seed()
        self.write(self.remote, "main.tex", "remote v1")
        original = s.DirectoryTransport.snapshot
        calls = []

        def moving_remote(transport):
            result = original(transport)
            calls.append(1)
            self.write(self.remote, "main.tex", "remote v2")
            return result
        with patch.object(s.DirectoryTransport, "snapshot", moving_remote), \
                patch.object(s.DirectoryTransport, "apply") as apply:
            self.sync.sync()
        self.assertEqual(len(calls), 1)
        apply.assert_not_called()
        self.assertEqual((self.local / "main.tex").read_text(), "remote v1")
        self.assertFalse((self.sync.state / "pending.json").exists())
        self.sync.sync()
        self.assertEqual((self.local / "main.tex").read_text(), "remote v2")

    def test_old_untouched_pull_recovers_latest_remote_and_archives_plan(self):
        self.interrupted_pull()
        old = (self.sync.state / "pending.json").read_bytes()
        self.write(self.remote, "main.tex", "remote v2")
        with patch.object(s.DirectoryTransport, "apply") as apply:
            report = self.sync.recover()
        apply.assert_not_called()
        self.assertEqual((self.local / "main.tex").read_text(), "remote v2")
        archive = Path(report["recovery_backup"])
        self.assertEqual((archive / "pending.json").read_bytes(), old)
        self.assertEqual((archive / "local/main.tex").read_text(), "one\ntwo\nthree\n")
        self.assertEqual((archive / "overleaf/main.tex").read_text(), "remote v2")
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_sync_can_resume_pending_pull_and_status_does_not_change_plan(self):
        self.interrupted_pull()
        self.write(self.remote, "main.tex", "remote v2")
        before = (self.sync.state / "pending.json").read_bytes()
        report = self.sync.sync(dry_run=True)
        self.assertTrue(report["recovery"])
        self.assertEqual(report["actions"][0]["side"], "local")
        self.assertEqual((self.sync.state / "pending.json").read_bytes(), before)
        self.assertFalse((self.sync.state / "recovery-backups").exists())
        self.assertEqual((self.local / "main.tex").read_text(), "one\ntwo\nthree\n")
        self.sync.sync()
        self.assertEqual((self.local / "main.tex").read_text(), "remote v2")

    def test_recovery_does_not_reuse_old_force_remote_choice(self):
        self.seed()
        self.write(self.local, "main.tex", "my edits")
        self.write(self.remote, "main.tex", "remote v1")
        with patch.object(self.sync, "apply_local", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.sync.sync(take_remote=["main.tex"])
        self.write(self.remote, "main.tex", "remote v2")
        with self.assertRaises(s.ConflictError) as caught:
            self.sync.recover()
        diff = "\n".join(caught.exception.report["conflicts"][0]["diff"])
        self.assertIn("-my edits", diff)
        self.assertIn("+remote v2", diff)
        self.assertEqual((self.local / "main.tex").read_text(), "my edits")

    def test_recovery_keeps_prior_choice_when_remote_content_is_unchanged(self):
        self.seed()
        self.write(self.local, "main.tex", "my edits")
        self.write(self.remote, "main.tex", "chosen remote")
        with patch.object(self.sync, "apply_local", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.sync.sync(take_remote=["main.tex"])
        self.sync.recover()
        self.assertEqual((self.local / "main.tex").read_text(), "chosen remote")
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_recovery_take_remote_preserves_new_local_edits_in_backup(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "my new edits")
        self.write(self.remote, "main.tex", "remote v2")
        report = self.sync.recover(take_remote=["main.tex"])
        self.assertEqual((self.local / "main.tex").read_text(), "remote v2")
        archive = Path(report["recovery_backup"])
        self.assertEqual((archive / "local/main.tex").read_text(), "my new edits")
        self.assertEqual((archive / "overleaf/main.tex").read_text(), "remote v2")

    def test_recovery_take_local_preserves_remote_and_updates_both(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "my new edits")
        self.write(self.remote, "main.tex", "remote v2")
        report = self.sync.recover(take_local=["main.tex"])
        self.assertEqual((self.remote / "main.tex").read_text(), "my new edits")
        self.assertEqual((Path(report["recovery_backup"]) / "overleaf/main.tex").read_text(), "remote v2")
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_recovery_cli_dry_run_with_choice_does_not_write(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "new edits")
        before = (self.sync.state / "pending.json").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = s.main(["recover", "--path", str(self.local), "--dry-run",
                           "--take-remote", "main.tex", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        self.assertEqual((self.sync.state / "pending.json").read_bytes(), before)
        self.assertEqual((self.local / "main.tex").read_text(), "new edits")

    def test_recovery_unresolved_file_aborts_other_choices(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "new local")
        self.write(self.remote, "main.tex", "new remote")
        self.write(self.local, "other.tex", "new other")
        before = (self.sync.state / "pending.json").read_bytes()
        with self.assertRaises(s.ConflictError) as caught:
            self.sync.recover(take_remote=["main.tex"])
        self.assertEqual(caught.exception.report["conflicts"][0]["path"], "other.tex")
        self.assertEqual((self.local / "main.tex").read_text(), "new local")
        self.assertEqual((self.sync.state / "pending.json").read_bytes(), before)
        self.assertFalse((self.sync.state / "recovery-backups").exists())

    def test_recovery_accepts_identical_new_edits_on_both_sides(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "identical new version")
        self.write(self.remote, "main.tex", "identical new version")
        self.sync.recover()
        self.assertEqual((self.local / "main.tex").read_text(), "identical new version")
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_recovery_explicit_remote_deletion_is_backed_up(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "my new edits")
        (self.remote / "main.tex").unlink()
        with self.assertRaises(s.ConflictError):
            self.sync.recover()
        report = self.sync.recover(take_remote=["main.tex"])
        self.assertFalse((self.local / "main.tex").exists())
        self.assertEqual((Path(report["recovery_backup"]) / "local/main.tex").read_text(), "my new edits")

    def test_local_edit_during_recovery_backup_retains_old_journal(self):
        self.interrupted_pull()
        self.write(self.remote, "main.tex", "remote v2")
        before = (self.sync.state / "pending.json").read_bytes()
        original = self.sync.archive_recovery

        def changed(*args):
            result = original(*args)
            self.write(self.local, "main.tex", "editing during backup")
            return result
        with patch.object(self.sync, "archive_recovery", changed), self.assertRaises(s.SyncError):
            self.sync.recover()
        self.assertEqual((self.sync.state / "pending.json").read_bytes(), before)
        self.assertEqual((self.local / "main.tex").read_text(), "editing during backup")

    def test_new_recovery_plan_can_recover_after_another_interruption(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "my new edits")
        self.write(self.remote, "main.tex", "remote v2")
        with patch.object(self.sync, "apply_local", side_effect=OSError("crash")), self.assertRaises(OSError):
            self.sync.recover(take_local=["main.tex"])
        self.assertEqual((self.remote / "main.tex").read_text(), "my new edits")
        self.sync.recover()
        self.assertFalse((self.sync.state / "pending.json").exists())

    def test_recovery_invalid_choices_keep_journal(self):
        self.interrupted_pull()
        self.write(self.local, "main.tex", "my new edits")
        before = (self.sync.state / "pending.json").read_bytes()
        for left, right in [(["main.tex"], ["main.tex"]), (["missing.tex"], [])]:
            with self.assertRaises(s.SyncError):
                self.sync.recover(take_local=left, take_remote=right)
        self.assertEqual((self.sync.state / "pending.json").read_bytes(), before)

    def test_pull_does_not_overwrite_local_edit_during_apply(self):
        self.seed()
        self.write(self.remote, "main.tex", "remote v1")
        original = self.sync.apply_local

        def edited(*args):
            self.write(self.local, "main.tex", "new local edit")
            return original(*args)
        with patch.object(self.sync, "apply_local", edited), self.assertRaises(s.SyncError):
            self.sync.sync()
        self.assertEqual((self.local / "main.tex").read_text(), "new local edit")
        self.assertTrue((self.sync.state / "pending.json").exists())

    def test_multiple_projects_are_isolated(self):
        other = Path(self.temp.name) / "other"
        remote = Path(self.temp.name) / "other_remote"
        remote.mkdir()
        s.initialize(other, str(remote))
        self.seed()
        self.write(other, "main.tex", "other")
        s.Synchronizer(other).sync()
        self.assertEqual((self.local / "main.tex").read_text(), "one\ntwo\nthree\n")

    def test_cli_conflict_exit_code_and_json(self):
        self.write(self.local, "main.tex", "local")
        self.write(self.remote, "main.tex", "remote")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = s.main(["sync", "--path", str(self.local), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["conflicts"][0]["path"], "main.tex")


class MergeTests(unittest.TestCase):
    def merge(self, base, left, right, name="main.tex"):
        value = s.merge_text(name, base.encode(), s.Entry(left.encode(), 1), s.Entry(right.encode(), 2))
        return value.data.decode() if value is not None else None

    def test_line_merge_cases(self):
        cases = [
            ("a\nb\nc\nd\n", "A\nb\nc\nd\n", "a\nb\nc\nD\n", "A\nb\nc\nD\n"),
            ("a\nb\nc\nd\n", "a\nB\nc\nd\n", "a\nb\nC\nd\n", "a\nB\nC\nd\n"),
            ("a\nb\nc\nd\n", "a\nnew\nb\nc\nd\n", "a\nb\nc\nother\nd\n", "a\nnew\nb\nc\nother\nd\n"),
            ("a\nb\nc\nd\n", "a\nc\nd\n", "a\nb\nc\nD\n", "a\nc\nD\n"),
            ("a\nb\nc\nd\ne\n", "a\nB\nc\nD\ne\n", "a\nB\nc\nd\nE\n", "a\nB\nc\nD\nE\n"),
            ("a\nb\nc\nd", "A\nb\nc\nd", "a\nb\nc\nd\n", "A\nb\nc\nd\n"),
            ("甲\n乙\n丙\n", "甲改\n乙\n丙\n", "甲\n乙\n丙改\n", "甲改\n乙\n丙改\n"),
            ("a\nb\nc\n", "a\nLEFT\nc\n", "a\nRIGHT\nc\n", None),
            ("a\nb\nc\n", "a\nnew\nb\nc\n", "a\nother\nb\nc\n", None),
            ("a\nb\nc\n", "a\nc\n", "a\nB\nc\n", None),
            ("a\nb\nc\nd\n", "a\nx\nd\n", "a\nb\nC\nd\n", None),
            ("a\nb\nc\n", "a\nnew\nb\nc\n", "a\nB\nc\n", None),
        ]
        for base, left, right, expected in cases:
            with self.subTest(base=base, left=left, right=right):
                self.assertEqual(self.merge(base, left, right), expected)
                self.assertEqual(self.merge(base, right, left), expected)

    def test_every_distinct_line_replacement_preserves_both_edits(self):
        base = [f"line {i}\n" for i in range(8)]
        for i in range(8):
            for j in range(8):
                if i == j:
                    continue
                left, right, expected = base.copy(), base.copy(), base.copy()
                left[i] = expected[i] = "LEFT\n"
                right[j] = expected[j] = "RIGHT\n"
                self.assertEqual(self.merge("".join(base), "".join(left), "".join(right)), "".join(expected))

    def test_same_line_different_words_still_requires_manual_merge(self):
        self.assertIsNone(self.merge("first middle last\n", "FIRST middle last\n", "first middle LAST\n"))

    def test_binary_unknown_and_oversized_text_are_not_merged(self):
        for name in ("image.png", "unknown.bin"):
            self.assertIsNone(self.merge("a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n", name))
        self.assertIsNone(self.merge("a\0\nb\nc\n", "A\0\nb\nc\n", "a\0\nb\nC\n"))
        self.assertIsNone(s.merge_text("main.tex", b"\xff", s.Entry(b"a", 1), s.Entry(b"b", 2)))
        with patch.object(s, "MAX_MERGE_BYTES", 3):
            self.assertIsNone(self.merge("a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n"))
        with patch.object(s, "MAX_MERGE_LINES", 2):
            self.assertIsNone(self.merge("a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n"))


class WebContractTests(unittest.TestCase):
    PID = "a" * 24
    FID = "b" * 24
    DID = "c" * 24

    def project(self):
        return {"_id": self.PID, "rootFolder": [{"_id": self.FID, "name": "rootFolder",
                  "docs": [{"_id": self.DID, "name": "main.tex"}], "folders": [], "fileRefs": []}]}

    def test_socket_io_ack_variants(self):
        payload = json.dumps([None, self.project()])
        for prefix in ("6:::1+", "431"):
            self.assertEqual(w.project_from_frame(prefix + payload, self.PID), self.project())
        self.assertIsNone(w.project_from_frame("2::", self.PID))
        self.assertIsNone(w.project_from_frame(payload, "d" * 24))

    def test_map_entity_ids(self):
        folders, entities = w.entity_map(self.project())
        self.assertEqual(folders[""], self.FID)
        self.assertEqual(entities["main.tex"]["id"], self.DID)

    def test_zip_nested_unicode_binary(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as z:
            z.writestr("figures/中文.pdf", b"%PDF\0")
            z.writestr("main.tex", "text\n")
        entries = w.unpack_zip(output.getvalue(), s.DEFAULT_EXCLUDES)
        self.assertEqual(entries["figures/中文.pdf"].data, b"%PDF\0")

    def test_zip_traversal_and_symlink_rejected(self):
        for name, symlink in [("../escape.tex", False), ("link.tex", True)]:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as z:
                info = zipfile.ZipInfo(name)
                if symlink:
                    info.external_attr = 0o120777 << 16
                z.writestr(info, "target")
            with self.assertRaises(s.SyncError):
                w.unpack_zip(output.getvalue(), [])

    def test_csrf_html_entities(self):
        parser = w.MetaParser()
        parser.feed('<meta name="ol-csrfToken" content="hello&amp;world">')
        self.assertEqual(parser.values["ol-csrfToken"], "hello&world")

    def test_upload_contract_and_delete(self):
        client = w.WebTransport("https://www.overleaf.com/project/" + self.PID, [])
        client.folders, client.entities = w.entity_map(self.project())
        calls = []
        class Response:
            def json(self):
                return {"success": True, "entity_id": WebContractTests.DID, "entity_type": "doc"}
        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            return Response()
        client.request = request
        client.put("main.tex", s.Entry(b"new", 1))
        self.assertEqual(calls[0][2]["multipart"]["name"], "main.tex")
        self.assertEqual(calls[0][2]["multipart"]["qqfile"]["buffer"], b"new")
        self.assertIn("folder_id=" + self.FID, calls[0][1])
        client.delete("main.tex")
        self.assertEqual(calls[1][:2], ("DELETE", f"/project/{self.PID}/doc/{self.DID}"))

    def test_web_apply_rechecks_before_each_write_and_final_state(self):
        client = w.WebTransport('https://www.overleaf.com/project/' + self.PID, [])
        client.folders, client.entities = w.entity_map(self.project())
        old = {'main.tex': s.Entry(b'old', 1)}
        desired = {'main.tex': s.Entry(b'new', 2), 'refs.bib': s.Entry(b'refs', 2)}
        live = dict(old)
        checks = []
        client.snapshot = lambda: checks.append(dict(live)) or dict(live)
        client.put = lambda name, entry: live.__setitem__(name, entry)
        client.delete = lambda name: live.pop(name)
        client.apply(old, desired)
        self.assertEqual(s.hashes(live), s.hashes(desired))
        self.assertEqual(len(checks), 3)

    def test_web_apply_stops_when_collaborator_edits_before_write(self):
        client = w.WebTransport('https://www.overleaf.com/project/' + self.PID, [])
        client.folders, client.entities = w.entity_map(self.project())
        old = {'main.tex': s.Entry(b'old', 1)}
        desired = {'main.tex': s.Entry(b'new', 2)}
        client.snapshot = lambda: {'main.tex': s.Entry(b'collaborator', 3)}
        with patch.object(client, 'put') as put, self.assertRaises(s.SyncError):
            client.apply(old, desired)
        put.assert_not_called()

    def test_web_apply_detects_server_conversion_after_write(self):
        client = w.WebTransport('https://www.overleaf.com/project/' + self.PID, [])
        client.folders, client.entities = w.entity_map(self.project())
        old = {'main.tex': s.Entry(b'old', 1)}
        desired = {'main.tex': s.Entry(b'new', 2)}
        snapshots = iter([old, {'main.tex': s.Entry(b'changed on server', 2)}])
        client.snapshot = lambda: next(snapshots)
        with patch.object(client, 'put'), self.assertRaises(s.SyncError):
            client.apply(old, desired)

    def test_no_paid_git_urls(self):
        value = s.normalize_remote(self.PID)
        self.assertEqual(value, "https://www.overleaf.com/project/" + self.PID)
        with self.assertRaises(s.SyncError):
            s.normalize_remote("https://git@git.overleaf.com/" + self.PID)


if __name__ == "__main__":
    unittest.main()
