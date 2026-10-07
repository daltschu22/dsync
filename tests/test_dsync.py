"""Regression tests; integration tests use only temporary local directories."""

import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import dsync


SCRIPT = Path(dsync.__file__).resolve()


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsync test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source ' $(touch INJECTED)"
        self.source.mkdir()
        self.dest = self.root / 'destination space'
        self.working = self.root / "work ' space"
        self.config = self.root / 'custom config.conf'
        self.config.write_text('[testremote]\ntype = alias\nremote = {}\n'.format(self.dest))

    def cli(self, *options, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source), str(self.dest),
             '-n', '3', '--working-dir', str(self.working), *map(str, options)],
            capture_output=True, text=True, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def fixture(self):
        files = ['plain', '.hidden', '#hash', ';semicolon', ' leading and trailing ',
                 "quote'$(touch INJECTED)", 'nested/deep/file', 'nested/.hidden']
        for index, name in enumerate(files):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('content {}\n'.format(index))
        (self.source / 'empty').mkdir()
        for name in ['.zfs', '.snapshot-old', 'nested/.snapshot']:
            folder = self.source / name
            folder.mkdir()
            (folder / 'excluded').write_text('excluded')
        return files

    def assert_copied(self, files):
        for name in files:
            self.assertEqual((self.dest / name).read_bytes(), (self.source / name).read_bytes())
        self.assertFalse((self.dest / '.zfs').exists())
        self.assertFalse((self.dest / '.snapshot-old').exists())
        self.assertFalse((self.dest / 'nested/.snapshot').exists())
        self.assertFalse((self.source / 'INJECTED').exists())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_rsync_basic_preserves_hidden_special_names_empty_dirs_and_symlinks(self):
        files = self.fixture()
        (self.source / 'line\nbreak').write_text('newline')
        files.append('line\nbreak')
        (self.source / 'link').symlink_to('plain')
        self.cli('--no-fpart')
        self.assert_copied(files)
        self.assertTrue((self.dest / 'empty').is_dir())
        self.assertEqual(os.readlink(self.dest / 'link'), 'plain')

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('fpart'), 'rsync and fpart required')
    def test_rsync_fpart_preserves_files_empty_dirs_and_symlinks(self):
        files = self.fixture()
        (self.source / 'line\nbreak').write_text('newline')
        files.append('line\nbreak')
        (self.source / 'link').symlink_to('plain')
        self.cli()
        self.assert_copied(files)
        self.assertTrue((self.dest / 'empty').is_dir())
        self.assertEqual(os.readlink(self.dest / 'link'), 'plain')

    @unittest.skipUnless(shutil.which('rclone'), 'rclone required')
    def test_rclone_basic_recursive_and_raw_names(self):
        files = self.fixture()
        self.cli('--no-fpart', '--cloud', '--rclone-config', self.config)
        self.assert_copied(files)

    @unittest.skipUnless(shutil.which('rclone') and shutil.which('fpart'), 'rclone and fpart required')
    def test_rclone_fpart(self):
        files = self.fixture()
        self.cli('--cloud', '--rclone-config', self.config)
        self.assert_copied(files)

    @unittest.skipUnless(shutil.which('rclone'), 'rclone required')
    def test_rclone_honors_config_and_preserves_existing_testfile(self):
        (self.source / 'file').write_text('data')
        self.dest.mkdir()
        (self.dest / 'testfile.dsync').write_text('keep me')
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source), 'testremote:', '-n', '2',
             '--working-dir', str(self.working), '--no-fpart', '--cloud',
             '--rclone-config', str(self.config)], capture_output=True, text=True, timeout=30,
            cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dest / 'file').read_text(), 'data')
        self.assertEqual((self.dest / 'testfile.dsync').read_text(), 'keep me')
        self.assertFalse((self.root / 'testremote:').exists())

    def test_dry_runs_do_not_create_destination(self):
        (self.source / 'file').write_text('data')
        for tool in ['rsync', 'rclone']:
            if not shutil.which(tool):
                continue
            with self.subTest(tool=tool):
                options = ['--no-fpart', '--dry-run']
                if tool == 'rclone':
                    options += ['--cloud', '--rclone-config', self.config]
                self.cli(*options)
                self.assertFalse(self.dest.exists())
                self.assertTrue((self.working / 'logs').is_dir())

    def test_nonpositive_counts_and_missing_arguments_fail(self):
        for count in ['0', '-1']:
            self.cli('--no-fpart', '-n', count, success=False)
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)

    def test_missing_source_and_invalid_host_files_fail(self):
        for content in ['', '# only a comment\n', '-oProxyCommand=evil', 'host;touch bad']:
            hosts = self.root / 'hosts'
            hosts.write_text(content)
            self.cli('--no-fpart', '--source-hosts', hosts, success=False)
        self.source.rmdir()
        self.cli('--no-fpart', success=False)

    def test_host_comments_and_whitespace(self):
        hosts = self.root / 'hosts'
        hosts.write_text('  # comment\n\n host-one \nuser@host.two\n')
        self.assertEqual(dsync.read_hosts(str(hosts)), ['host-one', 'user@host.two'])

    def test_overlap_rejected_before_creating_work(self):
        self.cli('--no-fpart', '--working-dir', self.source / 'work', success=False)
        self.assertFalse((self.source / 'work').exists())
        self.dest = self.source / 'dest'
        self.cli('--no-fpart', success=False)
        self.assertFalse(self.dest.exists())

    def test_work_directory_cannot_contain_source_or_destination(self):
        (self.source / 'keep').write_text('data')
        self.cli('--no-fpart', '--working-dir', self.root, success=False)
        self.assertEqual((self.source / 'keep').read_text(), 'data')
        self.dest = self.working / 'chunks'
        self.cli('--no-fpart', success=False)
        self.assertFalse(self.working.exists())

    def test_logs_cannot_be_deleted_by_chunk_replacement(self):
        self.cli('--no-fpart', '--log-output', self.working / 'chunks' / 'logs', success=False)
        self.assertFalse(self.working.exists())

    def test_unowned_chunks_survive_dry_run(self):
        chunks = self.working / 'chunks'
        chunks.mkdir(parents=True)
        (chunks / 'unrelated-backup').write_text('keep backup')
        (chunks / 'chunk.0').write_text('keep even a matching filename')
        (self.source / 'file').write_text('data')
        result = self.cli('--no-fpart', '--dry-run', success=False)
        self.assertIn('Refusing to replace working files', result.stderr)
        self.assertEqual((chunks / 'unrelated-backup').read_text(), 'keep backup')
        self.assertEqual((chunks / 'chunk.0').read_text(), 'keep even a matching filename')
        self.assertFalse(self.dest.exists())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_unrecognized_file_in_owned_chunks_preserves_previous_generation(self):
        (self.source / 'file').write_text('data')
        self.cli('--no-fpart')
        chunks = self.working / 'chunks'
        manifest = (self.working / 'manifest.json').read_bytes()
        previous = {path.name: path.read_bytes() for path in chunks.iterdir()}
        (chunks / 'unrelated-backup').write_text('keep backup')
        self.cli('--no-fpart', success=False)
        self.assertEqual((chunks / 'unrelated-backup').read_text(), 'keep backup')
        self.assertEqual((self.working / 'manifest.json').read_bytes(), manifest)
        for name, content in previous.items():
            self.assertEqual((chunks / name).read_bytes(), content)

    def test_chunk_symlink_is_refused_without_touching_target(self):
        self.working.mkdir()
        external = self.root / 'external'
        external.mkdir()
        (external / 'backup').write_text('keep')
        (self.working / 'chunks').symlink_to(external, target_is_directory=True)
        (self.source / 'file').write_text('data')
        self.cli('--no-fpart', '--dry-run', success=False)
        self.assertEqual((external / 'backup').read_text(), 'keep')
        self.assertTrue((self.working / 'chunks').is_symlink())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_changed_owned_chunk_is_not_deleted_on_regeneration(self):
        (self.source / 'file').write_text('data')
        self.cli('--no-fpart')
        chunk = next((self.working / 'chunks').iterdir())
        chunk.write_text('user replacement')
        self.cli('--no-fpart', success=False)
        self.assertEqual(chunk.read_text(), 'user replacement')

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_owned_chunks_can_be_regenerated_with_fewer_jobs(self):
        for index in range(4):
            (self.source / str(index)).write_text('data')
        self.cli('--no-fpart')
        self.assertEqual(len(list((self.working / 'chunks').iterdir())), 3)
        self.cli('--no-fpart', '-n', '1')
        self.assertEqual(len(list((self.working / 'chunks').iterdir())), 1)
        self.cli('--no-fpart', '--reuse')

    def test_remote_dest_does_not_create_local_directory(self):
        hosts = self.root / 'hosts'
        hosts.write_text('destination-host\n')
        (self.source / 'file').write_text('data')
        args = dsync.parse_arguments([str(self.source), str(self.dest), '-n', '1', '--no-fpart',
                                      '--working-dir', str(self.working), '--destination-hosts', str(hosts)])
        with mock.patch.object(dsync, 'run_transfers') as transfer, mock.patch.object(dsync, 'executable', side_effect=lambda name: name):
            dsync.run(args)
        transfer.assert_called_once()
        self.assertFalse(self.dest.exists())

    def test_remote_dest_preserves_controller_symlinks_and_parent_components(self):
        hosts = self.root / 'hosts'
        hosts.write_text('test-host\n')
        controller_only = self.root / 'controller-only'
        controller_only.mkdir()
        self.dest.symlink_to(controller_only, target_is_directory=True)
        (self.source / 'file').write_text('data')
        for host_option in ['--source-hosts', '--destination-hosts']:
            for suffix in ['', '/../backup']:
                with self.subTest(host_option=host_option, suffix=suffix):
                    destination = str(self.dest) + suffix
                    args = dsync.parse_arguments([str(self.source), destination, '-n', '1', '--no-fpart',
                                                  '--working-dir', str(self.working), host_option, str(hosts)])
                    with mock.patch.object(dsync, 'run_transfers') as transfer:
                        dsync.run(args)
                    self.assertEqual(transfer.call_args.args[4], destination)

    def test_worker_source_and_config_preserve_controller_symlinks(self):
        hosts = self.root / 'hosts'
        hosts.write_text('test-host\n')
        source_alias = self.root / 'source-alias'
        source_alias.symlink_to(self.source, target_is_directory=True)
        config_alias = self.root / 'config-alias'
        config_alias.symlink_to(self.config)
        (self.source / 'file').write_text('data')
        args = dsync.parse_arguments([str(source_alias), 'remote:bucket', '-n', '1', '--no-fpart', '--cloud',
                                      '--rclone-config', str(config_alias), '--working-dir', str(self.working),
                                      '--source-hosts', str(hosts)])
        with mock.patch.object(dsync, 'run_transfers') as transfer:
            dsync.run(args)
        self.assertEqual(transfer.call_args.args[3], source_alias)
        command = dsync.Rclone('rclone').build_command(args, source_alias, 'remote:bucket', 'test-host', None)
        remote = shlex.split(command[-1])
        self.assertEqual(remote[remote.index('--config') + 1], str(config_alias))

    def test_remote_dest_rejects_controller_relative_paths(self):
        hosts = self.root / 'hosts'
        hosts.write_text('test-host\n')
        self.dest = Path('relative-destination')
        result = self.cli('--no-fpart', '--destination-hosts', hosts, success=False)
        self.assertIn('must use an absolute path', result.stderr)
        self.assertFalse(self.working.exists())

    def test_basic_chunking_avoids_empty_jobs(self):
        (self.source / '.hidden').write_text('data')
        directory = self.root / 'parts'
        directory.mkdir()
        file_ops = dsync.FilesystemOps(self.source, self.working, self.working / 'logs')
        chunks = file_ops.no_fpart_chunk_gen(directory, 100, False)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].read_bytes(), b'./.hidden\0')

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_empty_source_has_no_jobs_and_is_reusable(self):
        self.cli('--no-fpart')
        self.cli('--no-fpart', '--reuse')
        self.assertFalse(self.dest.exists())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_reuse_validates_source_mode_and_chunk_integrity(self):
        (self.source / 'file').write_text('data')
        self.cli('--no-fpart')
        self.cli('--no-fpart', '--reuse', '-n', '1')
        self.cli('--reuse', success=False)  # Different chunk mode.
        original = self.source
        self.source = self.root / 'other-source'
        self.source.mkdir()
        self.cli('--no-fpart', '--reuse', success=False)
        self.source = original
        chunk = next((self.working / 'chunks').iterdir())
        chunk.write_bytes(b'tampered\0')
        self.cli('--no-fpart', '--reuse', success=False)

    def test_reuse_without_manifest_fails(self):
        self.cli('--no-fpart', '--reuse', success=False)

    def test_lock_rejects_competing_run(self):
        with dsync.working_lock(self.working):
            self.cli('--no-fpart', success=False)

    def test_cloud_rejects_newlines_and_retains_previous_manifest(self):
        args = dsync.parse_arguments([str(self.source), 'remote:', '-n', '2', '--cloud', '--no-fpart'])
        self.working.mkdir()
        logs = self.working / 'logs'
        logs.mkdir()
        (self.source / 'valid').write_text('data')
        file_ops = dsync.FilesystemOps(self.source, self.working, logs)
        previous = file_ops.prepare_chunks(args)
        manifest = (self.working / 'manifest.json').read_bytes()
        (self.source / 'bad\nname').write_text('data')
        with self.assertRaises(dsync.SyncError):
            file_ops.prepare_chunks(args)
        self.assertEqual((self.working / 'manifest.json').read_bytes(), manifest)
        self.assertTrue(previous[0].exists())

    def test_ssh_command_quotes_and_streams_chunk_on_stdin(self):
        args = dsync.parse_arguments([str(self.source), str(self.dest), '-n', '2', '--no-fpart'])
        with mock.patch.object(dsync, 'executable', return_value='/usr/bin/ssh'):
            command = dsync.Rsync('/custom/bin/rsync').build_command(
                args, self.source, str(self.dest), 'user@worker', 'storage')
        self.assertEqual(command[:5], ['/usr/bin/ssh', '-o', 'BatchMode=yes', '--', 'user@worker'])
        remote = shlex.split(command[5])
        self.assertEqual(remote[0], 'rsync')
        self.assertIn('--files-from=-', remote)
        self.assertEqual(remote[-2], str(self.source) + '/')
        self.assertEqual(remote[-1], 'storage:' + str(self.dest))

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('rclone'), 'transfer tools required')
    def test_source_host_execution_with_local_ssh_shim(self):
        files = self.fixture()
        hosts = self.root / 'hosts'
        hosts.write_text('test-worker\n')
        self.fake_tool('ssh', '''
            import subprocess, sys
            sys.exit(subprocess.call(['/bin/sh', '-c', sys.argv[-1]]))
        ''')
        with mock.patch.dict(os.environ, {'PATH': str(self.root / 'bin') + os.pathsep + os.environ['PATH']}):
            for cloud in [False, True]:
                with self.subTest(cloud=cloud):
                    options = ['--no-fpart', '--source-hosts', hosts]
                    if cloud:
                        options += ['--cloud', '--rclone-config', self.config]
                    self.cli(*options)
                    self.assert_copied(files)
                    shutil.rmtree(self.dest)

    def fake_tool(self, name, code):
        bindir = self.root / 'bin'
        bindir.mkdir(exist_ok=True)
        tool = bindir / name
        tool.write_text('#!{}\n'.format(sys.executable) + textwrap.dedent(code))
        tool.chmod(0o755)
        return tool

    def test_transfer_and_partition_failures_are_nonzero(self):
        self.fake_tool('rsync', 'import sys\nsys.exit(23)\n')
        self.fake_tool('fpart', 'import sys\nsys.exit(4)\n')
        (self.source / 'file').write_text('data')
        with mock.patch.dict(os.environ, {'PATH': str(self.root / 'bin') + os.pathsep + os.environ['PATH']}):
            result = self.cli('--no-fpart', success=False)
            self.assertIn('exit 23', result.stderr)
            result = self.cli(success=False)
            self.assertIn('fpart failed', result.stderr)

    def test_fpart_diagnostic_with_zero_exit_is_a_failure(self):
        self.fake_tool('fpart', "import sys\nprint('./unreadable: Permission denied', file=sys.stderr)\n")
        self.fake_tool('rsync', "raise AssertionError('transfer should not start')\n")
        (self.source / 'file').write_text('data')
        with mock.patch.dict(os.environ, {'PATH': str(self.root / 'bin') + os.pathsep + os.environ['PATH']}):
            result = self.cli(success=False)
        self.assertIn('fpart reported a diagnostic', result.stderr)
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.working / 'manifest.json').exists())

    @unittest.skipUnless(shutil.which('rsync') and shutil.which('fpart') and os.geteuid() != 0,
                         'rsync, fpart and a non-root user required')
    def test_fpart_unreadable_directory_cannot_silently_succeed(self):
        unreadable = self.source / 'unreadable'
        unreadable.mkdir()
        (unreadable / 'file').write_text('data')
        unreadable.chmod(0)
        try:
            self.cli(success=False)
            self.assertFalse(self.dest.exists())
        finally:
            unreadable.chmod(0o700)

    def test_bounded_concurrency_waits_for_completion(self):
        events = self.root / 'events'
        tool = self.fake_tool('rsync', '''
            import os, sys, time
            from pathlib import Path
            event = Path(os.environ['DSYNC_TEST_EVENTS'])
            with event.open('a') as log:
                log.write('start %s\\n' % os.getpid())
            sys.stdin.buffer.read()
            time.sleep(.15)
            with event.open('a') as log:
                log.write('end %s\\n' % os.getpid())
        ''')
        chunks = []
        for index in range(5):
            chunk = self.root / 'chunk.{}'.format(index)
            chunk.write_bytes(b'file\0')
            chunks.append(chunk)
        logs = self.root / 'logs'
        logs.mkdir()
        args = dsync.parse_arguments([str(self.source), str(self.dest), '-n', '2', '--no-fpart'])
        with mock.patch.dict(os.environ, {'DSYNC_TEST_EVENTS': str(events)}):
            dsync.run_transfers(args, chunks, dsync.Rsync(str(tool)), self.source, str(self.dest), [], [], logs)
        active = peak = started = 0
        for line in events.read_text().splitlines():
            if line.startswith('start'):
                active += 1
                started += 1
            else:
                active -= 1
            peak = max(peak, active)
        self.assertEqual(started, 5)
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)

    def test_interrupt_stops_transfer_process(self):
        self.assert_signal_stops_process(signal.SIGINT, 'rsync')

    def test_sigterm_stops_transfer_process(self):
        self.assert_signal_stops_process(signal.SIGTERM, 'rsync')

    def test_sigterm_stops_fpart_process(self):
        self.assert_signal_stops_process(signal.SIGTERM, 'fpart')

    def assert_signal_stops_process(self, signum, tool):
        pidfile = self.root / 'pid'
        self.fake_tool(tool, '''
            import os, time
            from pathlib import Path
            Path(os.environ['DSYNC_TEST_PID']).write_text(str(os.getpid()))
            time.sleep(30)
        ''')
        (self.source / 'file').write_text('data')
        env = dict(os.environ, PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'],
                   DSYNC_TEST_PID=str(pidfile))
        options = ['--no-fpart'] if tool == 'rsync' else []
        process = subprocess.Popen([sys.executable, str(SCRIPT), str(self.source), str(self.dest),
                                    '-n', '1', *options, '--working-dir', str(self.working)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pidfile.exists())
            process.send_signal(signum)
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 128 + signum, out + err)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            if pidfile.exists():
                try:
                    os.killpg(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_cancellation_waits_until_spawned_process_is_registered(self):
        processes = []
        fake_process = object()

        def spawn(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return fake_process

        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(dsync.SyncInterrupted):
            with dsync.cancellation_handlers(), mock.patch.object(dsync.subprocess, 'Popen', side_effect=spawn):
                dsync.start_process(['rsync'], processes)
        self.assertEqual(processes, [fake_process])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_signal_during_cleanup_does_not_abandon_process(self):
        ready = self.root / 'ready'
        child_code = '''
import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).touch()
time.sleep(30)
'''
        process = subprocess.Popen([sys.executable, '-c', child_code, str(ready)], start_new_session=True)
        signal_group = dsync.signal_group

        def interrupt_cleanup(process, signum):
            # Deliver a real signal at a deterministic point during cleanup.
            os.kill(os.getpid(), signal.SIGTERM)
            return signal_group(process, signum)

        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            with self.assertRaises(dsync.SyncInterrupted):
                with dsync.cancellation_handlers(), mock.patch.object(
                        dsync, 'signal_group', side_effect=interrupt_cleanup):
                    dsync.stop_processes([process], grace_seconds=.2)
            self.assertEqual(process.poll(), -signal.SIGKILL)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    def test_cleanup_kills_descendant_after_leader_exits(self):
        pidfile = self.root / 'grandchild.pid'
        child_code = '''
import os, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
'''
        parent_code = '''
import subprocess, sys, time
subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]])
time.sleep(30)
'''
        process = subprocess.Popen([sys.executable, '-c', parent_code, str(pidfile), child_code],
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(pidfile.exists())
            child_pid = int(pidfile.read_text())
            # The leader is already gone when cleanup begins. Its descendant
            # still owns the process group and ignores graceful termination.
            process.terminate()
            process.wait(timeout=5)
            dsync.stop_processes([process], grace_seconds=.1)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    state = Path('/proc/{}/stat'.format(child_pid)).read_text().rsplit(')', 1)[1].split()[0]
                except FileNotFoundError:
                    break
                if state == 'Z':  # An orphaned zombie is stopped; PID 1 must reap it.
                    break
                time.sleep(.02)
            else:
                self.fail('descendant survived process-group cleanup')
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


if __name__ == '__main__':
    unittest.main()
