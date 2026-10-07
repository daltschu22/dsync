#!/usr/bin/env python3
"""Partition a local filesystem and copy it with bounded parallel transfers."""

# Written by daltschu22 -- https://github.com/daltschu22

import argparse
from collections import OrderedDict
from contextlib import contextmanager
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


class SyncError(Exception):
    """An actionable transfer or configuration error."""


class SyncInterrupted(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum


class Cancellation:
    """Defer interruption while a newly spawned process is being registered."""

    def __init__(self):
        self.signum = None
        self.deferred = 0
        self.cleaning_up = False

    def __call__(self, signum, frame):
        if not self.cleaning_up:
            self.signum = self.signum or signum
            if not self.deferred:
                self.raise_if_pending()

    def raise_if_pending(self):
        if self.signum is not None and not self.cleaning_up:
            self.cleaning_up = True
            raise SyncInterrupted(self.signum)


@contextmanager
def cancellation_handlers():
    handler = Cancellation()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, handler)
        yield
    finally:
        for sig, old_handler in previous.items():
            signal.signal(sig, old_handler)


@contextmanager
def defer_cancellation():
    handler = signal.getsignal(signal.SIGTERM)
    if isinstance(handler, Cancellation):
        handler.deferred += 1
    try:
        yield
    finally:
        if isinstance(handler, Cancellation):
            handler.deferred -= 1
            if not handler.deferred:
                handler.raise_if_pending()


def start_process(command, processes, **kwargs):
    with defer_cancellation():
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        processes.append(process)
        return process


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be greater than zero')
    return number


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description='Partition a local source and copy its contents with parallel rsync/rclone processes.')
    parser.add_argument('source', help='Local source directory (contents are copied)')
    parser.add_argument('dest', help='Destination directory or rclone remote:path with --cloud')
    parser.add_argument('-n', '--number', type=positive_int, required=True,
                        help='Number of chunks and maximum concurrent transfer processes')
    parser.add_argument('--no-fpart', action='store_true',
                        help='Use basic chunking (top-level entries for rsync; recursive files for rclone)')
    parser.add_argument('--source-hosts', help='File containing SSH hosts on which to run transfers')
    parser.add_argument('--destination-hosts', help='File containing rsync destination SSH hosts')
    parser.add_argument('--reuse', action='store_true', help='Reuse validated chunks from the same source and mode')
    parser.add_argument('--cloud', action='store_true', help='Copy using rclone')
    parser.add_argument('--dry-run', action='store_true', help='Preview transfers without writing to the destination')
    parser.add_argument('--rclone-config', default='~/.config/rclone/rclone.conf', help='Rclone config file')
    parser.add_argument('--working-dir', default='~/dsync_working/', help='Directory for chunks and run state')
    parser.add_argument('--log-output', help='Log directory (default: WORKING_DIR/logs)')
    return parser.parse_args(argv)


def executable(name):
    path = shutil.which(name)
    if path is None:
        raise SyncError('{} is not installed or not on PATH'.format(name))
    return path


def local_path(value):
    return Path(value).expanduser().resolve()


def absolute_path(value):
    """Expand a controller-relative path without dereferencing its symlinks."""
    return Path(value).expanduser().absolute()


def ssh_options():
    # Use the same noninteractive, bounded connection on both SSH hops.
    return ['-T', '-o', 'StdinNull=no', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']


def read_hosts(filename):
    if not filename:
        return []
    hosts = []
    for line in local_path(filename).read_text().splitlines():
        host = line.strip()
        if not host or host.startswith('#'):
            continue
        # Hosts become SSH operands and rsync host:path prefixes, never shell code.
        if not re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*', host):
            raise SyncError('Invalid SSH host {!r} in {}'.format(host, filename))
        hosts.append(host)
    if not hosts:
        raise SyncError('Host file is empty: {}'.format(filename))
    return hosts


def inside(path, parent):
    return path == parent or parent in path.parents


@contextmanager
def working_lock(working):
    working.mkdir(parents=True, exist_ok=True)
    with (working / '.dsync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError('Another dsync run is using {}'.format(working)) from None
        yield


def excluded(name):
    return name == '.zfs' or name.startswith('.snapshot')


def basic_entries(source, cloud):
    if not cloud:
        with os.scandir(source) as entries:
            for entry in entries:
                if not excluded(entry.name):
                    # Rsync treats leading # and ; as comments, even with --from0.
                    yield b'./' + os.fsencode(entry.name)
        return

    # Walk without accumulating every filename in a wide directory. Keep one
    # scandir handle open and only queue directories; rclone skips symlinks.
    directories = [str(source)]
    while directories:
        directory = directories.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if excluded(entry.name) or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(entry.path)
                else:
                    yield os.fsencode(os.path.relpath(entry.path, source))


def cloud_entry(entry):
    if b'\n' in entry or b'\r' in entry:
        raise SyncError('Rclone chunk lists cannot represent newline/carriage-return filenames: {!r}'.format(entry))
    return entry + b'\n'


def nul_entries(path):
    pending = b''
    with path.open('rb') as handle:
        while True:
            block = handle.read(65536)
            if not block:
                break
            entries = (pending + block).split(b'\0')
            pending = entries.pop()
            yield from entries
    if pending:
        raise SyncError('Incomplete NUL-delimited chunk: {}'.format(path))


def signal_group(process, signum):
    # Reap the leader if possible, but its exit says nothing about descendants.
    process.poll()
    try:
        os.killpg(process.pid, signum)
        return True
    except ProcessLookupError:
        return False


def stop_processes(processes, grace_seconds=5):
    with defer_cancellation():
        groups = [process for process in processes if signal_group(process, signal.SIGTERM)]
        deadline = time.monotonic() + grace_seconds
        while groups and time.monotonic() < deadline:
            groups = [process for process in groups if signal_group(process, 0)]
            if groups:
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        for process in groups:
            signal_group(process, signal.SIGKILL)
        for process in processes:
            process.wait()


class Fpart:
    """Run fpart and prepare its file lists for the selected transfer tool."""

    def __init__(self):
        self.fpart_bin = executable('fpart')

    def run_fpart(self, command, source, out, err):
        processes = []
        try:
            process = start_process(command, processes, cwd=source, stdout=out, stderr=err)
            return process.wait()
        finally:
            stop_processes(processes)

    def generate_chunks(self, directory, source, number, cloud, logs):
        command = [self.fpart_bin, '-0', '-x', '.zfs', '-x', '.snapshot*',
                   '-n', str(number), '-o', str(directory / 'chunk')]
        if not cloud:
            command.append('-z')  # Preserve empty directories without recursively copying chunks twice.
        command.append('.')
        with (logs / 'fpart.out').open('wb') as out, (logs / 'fpart.err').open('wb') as err:
            status = self.run_fpart(command, source, out, err)
        if status:
            raise SyncError('fpart failed (exit {}); see {}'.format(status, logs / 'fpart.err'))
        # Fpart can return zero after filesystem traversal errors. Without verbose
        # flags its only normal stderr output is partition statistics. Fail closed
        # on other diagnostics instead of reporting an incomplete copy as success.
        with (logs / 'fpart.err').open('rb') as errors:
            for line in errors:
                if line.strip() and not re.fullmatch(rb'Part #\d+: size = \d+, files = \d+', line.strip()):
                    raise SyncError('fpart reported a diagnostic; see {}'.format(logs / 'fpart.err'))
        chunks = sorted(path for path in directory.iterdir()
                        if re.fullmatch(r'chunk\.\d+', path.name) and path.stat().st_size)
        if cloud:
            for path in chunks:
                converted = path.with_suffix(path.suffix + '.tmp')
                with converted.open('wb') as out:
                    for entry in nul_entries(path):
                        if entry.startswith(b'./'):
                            entry = entry[2:]
                        out.write(cloud_entry(entry))
                converted.replace(path)
        return chunks


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(65536), b''):
            result.update(block)
    return result.hexdigest()


def chunk_identity(source, args):
    stat = source.stat()
    return {'version': 1, 'source': str(source), 'device': stat.st_dev, 'inode': stat.st_ino,
            'cloud': args.cloud, 'no_fpart': args.no_fpart}


def read_manifest(path):
    if path.is_symlink():
        raise ValueError('manifest must not be a symlink')
    manifest = json.loads(path.read_text())
    identity = manifest['identity']
    if identity['version'] != 1 or not isinstance(identity['source'], str):
        raise ValueError('unrecognized manifest identity')
    for key in ('device', 'inode'):
        if type(identity[key]) is not int:
            raise ValueError('invalid source identity')
    for key in ('cloud', 'no_fpart'):
        if type(identity[key]) is not bool:
            raise ValueError('invalid chunking mode')
    for name, checksum in manifest['chunks'].items():
        if not re.fullmatch(r'chunk\.\d+', name) or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise ValueError('invalid chunk record')
    return manifest


class FilesystemOps:
    """Manage basic chunking, saved chunks, and their ownership checks."""

    def __init__(self, source, working_dir, log_dir):
        self.source = source
        self.working_dir = working_dir
        self.log_dir = log_dir

    def no_fpart_chunk_gen(self, directory, number, cloud):
        chunks = []
        handles = OrderedDict()
        try:
            for index, entry in enumerate(basic_entries(self.source, cloud)):
                slot = index % number
                if slot == len(chunks):
                    path = directory / 'chunk.{}'.format(slot)
                    chunks.append(path)
                handle = handles.pop(slot, None)
                if handle is None:
                    if len(handles) >= 32:
                        _, oldest = handles.popitem(last=False)
                        oldest.close()
                    handle = chunks[slot].open('ab')
                handles[slot] = handle
                handle.write(cloud_entry(entry) if cloud else entry + b'\0')
        finally:
            for handle in handles.values():
                handle.close()
        return chunks

    def owned_chunks(self):
        """Return only verified files that a previous dsync run created."""
        working = self.working_dir
        target = working / 'chunks'
        manifest_path = working / 'manifest.json'
        try:
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError('chunks must be a directory, not a file or symlink')
            entries = set(target.iterdir()) if target.exists() else set()
            if manifest_path.exists() or manifest_path.is_symlink():
                manifest = read_manifest(manifest_path)
                chunks = [target / name for name in manifest['chunks']]
                if entries != set(chunks):
                    raise ValueError('chunk directory contains unrecognized or missing files')
                for path in chunks:
                    if path.is_symlink() or not path.is_file() or digest(path) != manifest['chunks'][path.name]:
                        raise ValueError('chunk is not an unchanged dsync file: {}'.format(path.name))
                return chunks
            if entries:
                raise ValueError('existing chunks have no ownership manifest')
            return []
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise SyncError('Refusing to replace working files: {}. Use a new working directory or move '
                            'the existing files aside after reviewing them.'.format(error)) from error

    def check_existing_chunks(self, args):
        working = self.working_dir
        manifest_path = working / 'manifest.json'
        identity = chunk_identity(self.source, args)
        try:
            manifest = read_manifest(manifest_path)
            if manifest['identity'] != identity:
                raise ValueError('source or chunking mode differs')
            chunks = []
            for name, checksum in manifest['chunks'].items():
                path = working / 'chunks' / name
                if (working / 'chunks').is_symlink() or path.is_symlink() or digest(path) != checksum:
                    raise ValueError('chunk changed: {}'.format(name))
                chunks.append(path)
            return chunks
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise SyncError('Cannot reuse chunks: {}. Regenerate without --reuse; if working files '
                            'were modified, use a new working directory.'.format(error)) from error

    def prepare_chunks(self, args):
        source = self.source
        working = self.working_dir
        logs = self.log_dir
        manifest_path = working / 'manifest.json'
        identity = chunk_identity(source, args)
        if args.reuse:
            return self.check_existing_chunks(args)

        self.owned_chunks()  # Refuse unrelated data before doing any partition work.
        # Build in isolation; a failed partition must not replace the previous good set.
        with tempfile.TemporaryDirectory(prefix='.partition-', dir=working) as temp:
            directory = Path(temp)
            if args.no_fpart:
                chunks = self.no_fpart_chunk_gen(directory, args.number, args.cloud)
            else:
                chunks = Fpart().generate_chunks(directory, source, args.number, args.cloud, logs)
            manifest = {'identity': identity, 'chunks': {p.name: digest(p) for p in chunks}}
            new_manifest = directory / 'manifest.json'
            new_manifest.write_text(json.dumps(manifest, indent=2) + '\n')
            target = working / 'chunks'
            previous_chunks = self.owned_chunks()  # Recheck after the source scan.
            # Invalidate before replacing so interruption cannot leave reusable stale state.
            manifest_path.unlink(missing_ok=True)
            for path in previous_chunks:
                path.unlink()
            target.mkdir(exist_ok=True)
            for path in chunks:
                # Exclusive creation refuses any file that appeared since validation.
                os.link(path, target / path.name)
            new_manifest.replace(manifest_path)
            return [target / path.name for path in chunks]


class Rsync:
    """Build archive transfers from rsync's NUL-delimited chunk lists."""

    name = 'rsync'

    def __init__(self, binary):
        self.rsync_bin = binary

    def build_command(self, args, source, dest, source_host, dest_host):
        rsync_bin = Path(self.rsync_bin).name if source_host else self.rsync_bin
        command = [rsync_bin, '-av', '--protect-args', '--from0', '--files-from=-']
        if args.no_fpart:
            command += ['--recursive', '--exclude=.zfs', '--exclude=.snapshot*']
        if dest_host:
            # This SSH client runs on the worker when --source-hosts is used.
            command += ['--rsh', shlex.join(['ssh', *ssh_options()])]
            dest = '{}:{}'.format(dest_host, dest)
        return finish_transfer_command(command, args, source, dest, source_host)


class Rclone:
    """Build rclone copy transfers with the requested configuration."""

    name = 'rclone'

    # Executed on the host reading the source. Rclone silently ignores missing
    # --files-from entries, so validate first and give it a rewindable file list.
    # The temporary file is unlinked automatically; exec keeps only its stdin fd.
    source_check = '''import os, sys, tempfile
try:
    source = os.fsencode(sys.argv[1])
    with tempfile.TemporaryFile() as files:
        for line in sys.stdin.buffer:
            entry = line[:-1] if line.endswith(b'\\n') else line
            if entry:
                os.lstat(os.path.join(source, entry))
            files.write(line)
        files.seek(0)
        os.dup2(files.fileno(), 0)
    os.execvp(sys.argv[2], sys.argv[2:])
except OSError as error:
    print('ERROR: rclone source check or launch failed: {}'.format(error), file=sys.stderr)
    sys.exit(1)
'''

    def __init__(self, binary):
        self.rclone_bin = binary
        self.threads = 2

    def build_command(self, args, source, dest, source_host, dest_host):
        rclone_bin = Path(self.rclone_bin).name if source_host else self.rclone_bin
        config = absolute_path(args.rclone_config) if source_host else local_path(args.rclone_config)
        command = [rclone_bin, 'copy', '-v', '--transfers', str(self.threads),
                   '--ask-password=false', '--config', str(config), '--files-from-raw', '-']
        return finish_transfer_command(command, args, source, dest, source_host,
                                       source_check=self.source_check)


def finish_transfer_command(command, args, source, dest, source_host, source_check=None):
    if args.dry_run:
        command.append('--dry-run')
    command += ['--', os.path.join(str(source), ''), dest]
    if source_check:
        python = 'python3' if source_host else sys.executable
        command = [python, '-c', source_check, str(source), *command]
    if source_host:
        command = [executable('ssh'), *ssh_options(), '--', source_host,
                   ' '.join(shlex.quote(arg) for arg in command)]
    return command


def run_transfers(args, chunks, transfer, source, dest, source_hosts, dest_hosts, logs):
    source_cycle = itertools.cycle(source_hosts or [None])
    dest_cycle = itertools.cycle(dest_hosts or [None])
    jobs = iter(enumerate(chunks))
    active = []
    processes = []
    failures = []
    exhausted = False
    tool = transfer.name
    try:
        while active or not exhausted:
            while len(active) < args.number and not exhausted:
                try:
                    index, chunk = next(jobs)
                except StopIteration:
                    exhausted = True
                    break
                command = transfer.build_command(args, source, dest,
                                                 next(source_cycle), next(dest_cycle))
                error_path = logs / '{}.err.{}'.format(tool, index)
                with chunk.open('rb') as files, (logs / '{}.out.{}'.format(tool, index)).open('wb') as out, error_path.open('wb') as err:
                    process = start_process(command, processes, stdin=files, stdout=out, stderr=err)
                active.append((process, error_path))
            pending = []
            for process, error_path in active:
                status = process.poll()
                if status is None:
                    pending.append((process, error_path))
                elif status:
                    failures.append('exit {}: {}'.format(status, error_path))
            # Retain groups with surviving descendants even after their leader exits.
            processes = [process for process in processes
                         if process.poll() is None or signal_group(process, 0)]
            active = pending
            if active:
                time.sleep(0.05)
    finally:
        stop_processes(processes)
    if failures:
        raise SyncError('{} transfer(s) failed; {}'.format(len(failures), '; '.join(failures)))


def run(args):
    # Check paths and host lists before creating working files.
    source = local_path(args.source)
    working = local_path(args.working_dir)
    logs = local_path(args.log_output) if args.log_output else working / 'logs'
    if inside(logs, working / 'chunks'):
        raise SyncError('Log directory must not be inside the reserved working chunks directory')
    if not source.is_dir():
        raise SyncError('Source is not a directory: {}'.format(source))
    source_hosts = read_hosts(args.source_hosts)
    dest_hosts = read_hosts(args.destination_hosts)
    if args.cloud and dest_hosts:
        raise SyncError('--destination-hosts applies only to rsync, not --cloud')
    if any(inside(path, source) or inside(source, path) for path in (working, logs)):
        raise SyncError('Working/log directories and the source tree must not overlap')
    cloud_remote = args.cloud and ':' in args.dest and not os.path.isabs(args.dest)
    remote_filesystem = bool(source_hosts or dest_hosts) and not cloud_remote
    if cloud_remote:
        dest = args.dest
    elif remote_filesystem:
        if not os.path.isabs(args.dest):
            raise SyncError('Remote filesystem destinations must use an absolute path')
        dest = args.dest  # Only the destination host can resolve its symlinks.
    else:
        if not os.path.isabs(os.path.expanduser(args.dest)) and ':' in args.dest:
            raise SyncError('Use --destination-hosts for remote rsync destinations')
        dest = str(local_path(args.dest))
    if not cloud_remote and not remote_filesystem:
        destination = local_path(dest)
        if inside(destination, source) or inside(source, destination):
            raise SyncError('Source and destination directories must not overlap')
        if any(inside(path, destination) or inside(destination, path) for path in (working, logs)):
            raise SyncError('Working/log directories and the destination tree must not overlap')
    # Choose the transfer tool; source workers use their own PATH.
    tool = 'rclone' if args.cloud else 'rsync'
    binary = tool if source_hosts else executable(tool)
    if args.cloud:
        transfer = Rclone(binary)
    else:
        transfer = Rsync(binary)
    if source_hosts or dest_hosts:
        executable('ssh')

    file_ops = FilesystemOps(source, working, logs)
    with working_lock(working):
        logs.mkdir(parents=True, exist_ok=True)
        # Generate chunks, or validate the saved file lists.
        chunks = file_ops.prepare_chunks(args)
        print('-- {} {} chunk(s), up to {} concurrent transfers'.format(
            'Reusing' if args.reuse else 'Prepared', len(chunks), args.number), flush=True)
        if args.dry_run:
            print('-- Dry run: destination will not be changed', flush=True)
        if not chunks:
            print('-- No entries to transfer')
            return
        if not args.cloud and not dest_hosts and not source_hosts and not args.dry_run:
            Path(dest).mkdir(parents=True, exist_ok=True)
        # Copy the chunks and wait for every transfer to finish.
        transfer_source = absolute_path(args.source) if source_hosts else source
        run_transfers(args, chunks, transfer, transfer_source, dest, source_hosts, dest_hosts, logs)
        print('-- {} completed successfully; logs: {}'.format('Dry run' if args.dry_run else 'Transfer', logs))


def main(argv=None):
    args = parse_arguments(argv)
    try:
        with cancellation_handlers():
            run(args)
    except SyncInterrupted as error:
        print('ERROR: Interrupted by {}; active local process groups stopped'.format(
            signal.Signals(error.signum).name), file=sys.stderr)
        return 128 + error.signum
    except KeyboardInterrupt:
        print('ERROR: Interrupted; active local transfer processes stopped', file=sys.stderr)
        return 130
    except (SyncError, OSError) as error:
        print('ERROR: {}'.format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
