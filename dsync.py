#!/usr/bin/env python

#Written by daltschu11 

import sys
import os
import argparse
from shutil import which
import glob
import subprocess
import pprint
import checkpyversion

pp = pprint.PrettyPrinter(indent=4)

def check_linux():
    if sys.platform != "linux" and sys.platform != "linux2":
        print("ERROR: Must run this on a linux machine")
        print(sys.platform)
        sys.exit()

def parse_arguments():
    parser = argparse.ArgumentParser(description="Uses fpart to bag up filesystems into defined chunks, \
        then transfers them using rsync/rclone either on a single host") # or clustered")
    parser.add_argument('source', metavar='/source/path/', help='Source path -- use absolute paths! \
        (dsync always behaves as if you used a trailing slash in rsync!)')
    parser.add_argument('dest', metavar='/destination/path/ OR cloud-prefix:bucket-name/path/in/bucket/', help='Destination path -- use absolute paths!')
    parser.add_argument('-n', '--number', type=int, action='store', required=True, help='Pack files into <num> chunks and kickoff <num> transfers')
    parser.add_argument('--no-fpart', action='store_true', required=False, help='Run without fpart in basic mode (Chunks consist of top level files/dirs) \
        WARNING: BROKEN WITH RCLONE, ONLY TRANSFERS FILES!')
    #parser.add_argument('--fpart-options', action='store', required=False, help='Override the default fpart options (list those here)')
    parser.add_argument('--source-hosts', action='store', required=False, help='Provide a file with a list of hosts you want to run the transfers to run on \
        (will evenly balance out the # of transfers with the number of hosts)')
    # parser.add_argument('--destination-hosts', action='store', required=False, help='Provide a file with a list of hosts you want the transfers to run against \
    #    (For example if you have a number of remote hosts with an NFS storage mount)')
    parser.add_argument('--reuse', action='store_true', required=False, help='Reuse existing chunk files from same source, and same working directory')
    parser.add_argument('--cloud', action='store_true', required=False, help='Upload data to a cloud provider using rclone instead of local rsync')
    parser.add_argument('--rclone-config', type=str, action='store', required=False, help='Path to config file for rclone (Must contain proper config!)')
    parser.add_argument('--dry-run', action='store_true', required=False, help='Run rclone or rsync in dry run mode (Wont actually copy anything)')
    parser.add_argument(
        '--working-dir',
        action='store',
        required=False,
        default=os.path.expanduser('~/dsync_working/'),
        metavar='/working/dir/',
        help='Directory in which temp files will be stored while running (default is your home dir ~/dsync_working/)'
        )
    parser.add_argument(
        '--log-output',
        action='store',
        required=False,
        metavar='/log/dir/',
        default=os.path.expanduser('~/dsync_working/logs/'),
        help='location for the log files (Default is in the working directory ~/dsync_working/logs/)'
        )

    if len(sys.argv[2:]) == 0:
        parser.print_help()
        parser.exit()

    return parser.parse_args()

class Rsync:
    # Class for running rsync

    def __init__(self):
        self.check_rsync()

    def check_rsync(self): #Check if rsync binary exists.
        self.rsync_bin = which('rsync')
        if self.rsync_bin is None:
            print("ERROR: rsync not installed!")
            sys.exit()

    def run_rsync(self, rsync_command, log_dir, log_stdout_path, log_stderr_path): #Run rsync with given paths.
        try:
            with open(log_stdout_path, 'w') as out, open(log_stderr_path, 'w') as err:
                subprocess.Popen(rsync_command, shell=True, stdout=out, stderr=err)
        except subprocess.CalledProcessError:
            print("ERROR: Something went wrong when running rsync!")
            sys.exit()
        except Exception as e:
            print(e)
            sys.exit()

    def sync_chunks(self, chunks, source, dest, log_dir, **kwargs): #Run through chunks from fpart and pass to run_rsync() to be ran. 
        x = 0
        for chunk in chunks:
            log_stdout_path = str(log_dir + 'rsync.out.' + str(x))
            log_stderr_path = str(log_dir + 'rsync.err.' + str(x))

            rsync_base_list = [
                self.rsync_bin,
                'copy',
            ]

            rsync_args_list = [
                '-avv',
                '--recursive',
                '--files-from %s' % (chunk),
            ]

            rsync_source_dest = [
                source,
                dest
            ]

            if 'dry_run_yesno' in kwargs:
                rsync_args_list.append('--dry-run')

            if 'list_of_source_hosts' in kwargs:
                list_of_source_hosts = kwargs.get('list_of_source_hosts')
                rsync_command_list = [
                    'ssh',
                    list_of_source_hosts[x],
                    "'"
                ]
                rsync_command_list.extend(rsync_base_list)
                rsync_command_list.extend(rsync_args_list)
                rsync_command_list.extend(rsync_source_dest)
                rsync_command_list.append("'")

            else:
                rsync_command_list = rsync_base_list + rsync_args_list +rsync_source_dest


            rsync_command = ' '.join(rsync_command_list)

            # print('-- ' + rsync_command) # Used for testing if you dont want to actually run the commands
            self.run_rsync(rsync_command, log_dir, log_stdout_path, log_stderr_path)
            x += 1

            # subprocess.call(['ps -ef | grep /usr/bin/rsync | grep chunk | grep -v grep'], shell=True)

class Fpart:
    # Class for running fpart

    def __init__(self):
        self.check_fpart()

    def check_fpart(self): #Check if fpart binary exists.
        self.fpart_bin = which('fpart')
        if self.fpart_bin is None:
            print("ERROR: fpart not installed!")
            sys.exit()

    def run_fpart(self, fpart_command, source, log_dir): #Run fpart against the given path.
        log_stdout_path = str(log_dir + 'fpart.out')
        log_stderr_path = str(log_dir + 'fpart.err')
        try:
            with open(log_stdout_path, 'w') as out, open(log_stderr_path, 'w') as err:
                process = subprocess.Popen(fpart_command, cwd=source, shell=True, stderr=err, stdout=out)
                process.wait()
        except subprocess.CalledProcessError:
            print("ERROR: Something went wrong when running fpart!")
            sys.exit()
        except Exception as e:
            print(e)
            sys.exit()

    def generate_chunks(self, file_ops, working_dir, thread_num, source, log_dir):
        #Delete existing chunks
        file_ops.delete_chunks(working_dir)
        # Assemble fpart arguments and run fpart
        chunk_path = working_dir + 'chunk' 
        fpart_command = ' '.join([self.fpart_bin, '-Z', '-x .zfs -x .snapshot*', '-n', str(thread_num), '-o', chunk_path, '.'])

        self.run_fpart(fpart_command, source, log_dir) #Run fpart to create chunk files.

        chunk_pattern = 'chunk*'
        chunks = file_ops.list_files_byname(working_dir, chunk_pattern)
        chunk_count = len(chunks)

        return chunk_count, chunks

class Rclone:
    # Class for running rclone

    def __init__(self):
        self.check_rclone()
        self.threads = 2

    def check_rclone(self): #Check if rclone binary exists.
        self.rclone_bin = which('rclone')
        if self.rclone_bin is None:
            print("ERROR: rclone not installed!")
            sys.exit()

    def run_rclone(self, rclone_command, log_dir, log_stdout_path, log_stderr_path):
        try:
            with open(log_stdout_path, 'w') as out, open(log_stderr_path, 'w') as err:
                subprocess.Popen(rclone_command, shell=True, stderr=err, stdout=out)
        except subprocess.CalledProcessError:
            print("ERROR: Something went wrong when running rclone!")
            sys.exit()
        except Exception as e:
            print(e)
            sys.exit()

    def sync_chunks(self, chunks, source, dest, log_dir, **kwargs):
        x = 0
        for chunk in chunks:
            log_stdout_path = str(log_dir + 'rclone.out.' + str(x))
            log_stderr_path = str(log_dir + 'rclone.err.' + str(x))

            rclone_base_list = [
                self.rclone_bin,
                'copy',
            ]

            rclone_args_list = [
                '-vvv',
                '--transfers',
                str(self.threads),
                '--files-from %s' % (chunk),
            ]

            rclone_source_dest = [
                source,
                dest
            ]

            if 'dry_run_yesno' in kwargs:
                rclone_args_list.append('--dry-run')

            if 'list_of_source_hosts' in kwargs:
                list_of_source_hosts = kwargs.get('list_of_source_hosts')
                rclone_command_list = [
                    'ssh',
                    list_of_source_hosts[x],
                    "'"
                ]
                rclone_command_list.extend(rclone_base_list)
                rclone_command_list.extend(rclone_args_list)
                rclone_command_list.extend(rclone_source_dest)
                rclone_command_list.append("'")

            else:
                rclone_command_list = rclone_base_list + rclone_args_list + rclone_source_dest

            rclone_command = ' '.join(rclone_command_list)

            # print('-- ' + rclone_command) #Used for testing if you dont want to actually run the commands
            self.run_rclone(rclone_command, log_dir, log_stdout_path, log_stderr_path)
            x += 1

    def test_write_perms(self, dest, log_dir): #Touches a file to remote to check permissions so you can fail before kicking off all the rclones
        log_stdout_path = str(log_dir + 'check_write_perms.out')
        log_stderr_path = str(log_dir + 'check_write_perms.err')
        test_file_path = str(dest + 'testfile.dsync')
        rclone_command = ' '.join([self.rclone_bin, 'touch', '-vvv', test_file_path])

        self.run_rclone(rclone_command, log_dir, log_stdout_path, log_stderr_path)

        #Cleanup test file
        self.cleanup_write_perms_test(dest, log_dir, test_file_path)

    def cleanup_write_perms_test(self, dest, log_dir, test_file_path): #Cleanup test touch file
        print("     + Removing test file")
        log_stdout_path = str(log_dir + 'cleanup_write_perms.out')
        log_stderr_path = str(log_dir + 'cleanup_write_perms.err')
        rclone_command = ' '.join([self.rclone_bin, 'deletefile', '-vv', test_file_path])

        self.run_rclone(rclone_command, log_dir, log_stdout_path, log_stderr_path)

    def clean_fpart_chunks(self, chunks):
        for chunk in chunks:
            sed_command = ' '.join(['sed -i \'s|^./||\'', chunk])
            process = subprocess.Popen(sed_command, shell=True)
            process.wait()

class Filesystem_Ops():
    # Class for running various filesystem operations
    
    def make_path(self, make_dir): #Check if the path exists and if not, create it. 
        try:
            print('     + ' + make_dir + " does not exist. making...")
            os.makedirs(make_dir)
        except OSError as e:
            if "Permission denied" in str(e):
                print("ERROR: Cannot create path {} due to permissions".format(make_dir))
            else:
                print(e)
            sys.exit()

    def check_path(self, dir_path): #Check if the path exists, if it does return True, if it doesnt return False.
        if os.path.exists(dir_path):
            return True
        else:
            return False

    def check_tilde(self, dir_path): #Check if path starts with '~'. If so, expand it.
        if dir_path.startswith('~'):
            dir_path = os.path.expanduser(dir_path)

        return dir_path

    def trailing_slash(self, dir_path): #Check if path has trailing slash, if it doesnt, add one.
        dir_path = os.path.join(dir_path, '')
        return dir_path

    def list_files_byname(self, dir_path, pattern): #List all files in a directory that match pattern.
        glob_name = dir_path + pattern
        file_list = glob.glob(glob_name)

        return file_list

    def check_read_perms(self, path):
        access = os.access(path, os.R_OK)

        return access

    def check_write_perms(self, path):
        access = os.access(path, os.W_OK)

        return access

    def read_file_into_list(self, file):
        try:
            with open(file, 'r') as f:
                lines = f.read().splitlines()
        except IOError as err:
            print("ERROR: Cannot read or file doesnt exist! {0}: {1}".format(file, err))
            sys.exit()

        return lines

    def delete_chunks(self, working_dir):
        chunk_pattern = 'chunk*'
        chunks = self.list_files_byname(working_dir, chunk_pattern)
        print("     + Removing previous chunk files...")
        for chunk in chunks:
            os.remove(chunk)

    def no_fpart_chunk_gen(self, working_dir, source, thread_num):
        depth = '*' #Setting a default of 1 levels deep just for now
        path = str(source + depth)
        file_list = [os.path.basename(x) for x in glob.glob(path)]

        chunks = [file_list[i::thread_num] for i in range(thread_num)]

        x = 0
        chunk_name_list = []
        for chunk in chunks:
            chunk_name = working_dir + 'chunk.' + str(x)
            chunk_name_list.append(chunk_name)
            
            with open(chunk_name, 'w') as f:
                for line in chunk:
                    f.write("%s\n" % line)
            x += 1

        return chunk_name_list

    def check_existing_chunks(self, working_dir, source): #Check if chunks already exist. 
        chunk_pattern = 'chunk*'
        chunks = self.list_files_byname(working_dir, chunk_pattern)
        chunk_count = 0
        true_false = False
        if chunks: #Check that there arent 0 chunks.
            first_chunk_file = open(chunks[0], 'r')
            if source in first_chunk_file.read(): #Check if the source directory is present inside the chunk file.
                chunk_count = len(chunks)
                true_false = True

        return true_false, chunk_count, chunks

def main():
    args = parse_arguments() #Parse arguments
    
    checkpyversion.check_py_version() #Check that you are running python3
    check_linux() #Check that you are running on linux

    file_ops = Filesystem_Ops()

    #Set variables from cmd line arguments
    source = file_ops.trailing_slash(file_ops.check_tilde(args.source))
    dest = file_ops.trailing_slash(file_ops.check_tilde(args.dest))
    thread_num = args.number
    # remote = args.remote
    no_fpart = args.no_fpart
    reuse = args.reuse
    working_dir = file_ops.trailing_slash(file_ops.check_tilde(args.working_dir))
    log_dir = file_ops.trailing_slash(file_ops.check_tilde(args.log_output))
    to_cloud = args.cloud
    rclone_config = args.rclone_config
    dry_run_yesno = args.dry_run
    source_hosts = args.source_hosts

    #Initialize fpart class.
    if not no_fpart:
        fpart_class = Fpart()

    #If to_cloud is true, use rclone. If not using cloud, use rsync.
    if to_cloud:
        rclone_class = Rclone()
    elif not to_cloud:
        rsync_class = Rsync()

    print("Checking Paths...")
    #Check if source exists, if not exit. 
    if not file_ops.check_path(source):
        print("ERROR: Source path does not exist!")
        sys.exit()

    source_read_access = file_ops.check_read_perms(source) #Check if you have read perms on source
    if not source_read_access:
        print("WARNING: You seem to not have read permissions on the source!")

    #Check if working paths exist, if not make them
    if not file_ops.check_path(working_dir):
        file_ops.make_path(working_dir)
    if not file_ops.check_path(log_dir):
        file_ops.make_path(log_dir)

    if not to_cloud: #Only run the destination check/creation if using rsync.
        if not file_ops.check_path(dest):
            file_ops.make_path(dest)

    # Check if not using fpart or reusing existing chunks
    if reuse: #If reuse is true, use existing chunk files
        reuse_true_false, chunk_count, chunks = file_ops.check_existing_chunks(working_dir, source) #If chunks exist, use those instead of generating.
        if reuse_true_false:
            print("Reusing {0} existing chunk files... (Thread count will be changed to {0})".format(str(chunk_count)))
        elif not reuse_true_false:
            print("The existing chunks dont match the source directory or dont exist!")
            sys.exit()
    else:
        if no_fpart: #Run without fpart (list files/dirs 2 dirs deep)
            print("Breaking source directory into chunks...")
            chunks = file_ops.no_fpart_chunk_gen(working_dir, source, thread_num)
        if not no_fpart:
            print("Running fpart to break source directory into chunks...")
            chunk_count, chunks = fpart_class.generate_chunks(file_ops, working_dir, thread_num, source, log_dir) #Generate chunks.

    if source_hosts: #Get a list of the source hosts
        print("Using a list of source hosts...")
        list_of_source_hosts = file_ops.read_file_into_list(source_hosts)

    if dry_run_yesno: #Warn the user that no files will be transferred
        print("WARNING: --dry-run used (NO FILES WILL ACTUALLY BE TRANSFERRED!)")

    if to_cloud: #If you are running a cloud transfer
        print("Using rclone...")
        rclone_class.rclone_config_file=rclone_config #Add the rclone config file to the class

        print("     + Testing rclone write permissions to bucket")
        rclone_class.test_write_perms(dest, log_dir)

        if not no_fpart:
            print("     + Cleaning up fpart chunks (remove './')")
            rclone_class.clean_fpart_chunks(chunks)

        print("     + Running rclone's...")
        rclone_optional_args = {} #kwargs dictionary for any optional stuff
        if dry_run_yesno:
            rclone_optional_args.update({"dry_run_yesno" : dry_run_yesno}) #Add dry run flag to kwargs
        if source_hosts:
            rclone_optional_args.update({"list_of_source_hosts" : list_of_source_hosts}) #Add list of source hosts to kwargs

        rclone_class.sync_chunks(chunks, source, dest, log_dir, **rclone_optional_args)
        
    else: #If you are running a local transfer
        print("Using rsync...")
        print("     + Running rsync's...")
        rsync_optional_args = {} #kwargs dictionary for any optional stuff
        if dry_run_yesno:
            rsync_optional_args.update({"dry_run_yesno" : dry_run_yesno}) #Add dry run flag to kwargs
        if source_hosts:
            rsync_optional_args.update({"list_of_source_hosts" : list_of_source_hosts}) #Add list of source hosts to kwargs

        rsync_class.sync_chunks(chunks, source, dest, log_dir, **rsync_optional_args) #Run rsync





if __name__ == "__main__":
    main()









if __name__ == "__main__":
    main()
