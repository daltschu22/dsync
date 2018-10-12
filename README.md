# dsync
dsync is a python script developed to move data, fast. It utilizes fpart(https://github.com/martymac/fpart) to break apart the source directory so that a copy can be run accross multiple instances of rsync(https://rsync.samba.org/) or rclone(rclone.org) on a single, or multiple hosts.

## Getting Started

These are the requirements and necessary steps in order to get you up and running with dsync.

### Requirements and prerequisites

```
Python 3+
fpart (https://github.com/martymac/fpart)
rsync(https://rsync.samba.org/)
rclone(rclone.org)
```

## Installing and Configuring

Clone the repo or download the necessary files.
```
git clone https://github.com/daltschu11/dsync.git

```

Install fpart, rsync, and rclone if using one or all.
 
Rsync should already be installed on most linux systems. 
Fpart instructions can be found in the README: https://github.com/martymac/fpart/blob/master/README
rclone installation instructions can be found here: https://rclone.org/downloads/

### Configuring rclone

You need to have an endpoint configured if you plan on running dsync with rclone.

The documentation for that is here: https://rclone.org/commands/rclone_config/
But, its fairly simple.

And example of a section of the config file for a Google Cloud Storage configuration is here: 
```
[google-cloud-bucket-1]
type = google cloud storage
client_id =
client_secret =
project_number =
service_account_file = /path/to/my/json-file.json
object_acl =
bucket_acl =
location = us
storage_class = COLDLINE
```

Or you can run through the config interactively and select n for new config:

```
$ rclone config
Current remotes:

Name                 Type
====                 ====


e) Edit existing remote
n) New remote
d) Delete remote
r) Rename remote
c) Copy remote
s) Set configuration password
q) Quit config

e/n/d/r/c/s/q> n
```

## Running dsync

dsync is run with a defined number of threads, a source, and a destination. These are the required arguments. 
 If defaults are used, the working directory that dsync uses to store working files is ~/dsync_working/.
 If defaults are used, the log directory is stored inside the working directory.
 dsync will default to using rsync (For local NFS transfers).
 dsync will also default to using fpart to chunk out the source directory. 

Default flags for rsync is `-av` which means rsync runs in `archive` mode. 
Default flags for rclone is `-v`. rclone will default to using the `copy` command. Which means source data will only be copied, not moved or deleted. 
Fpart will default to ignoring `.zfs` and `.snapshot*` directories.

If dsync is run again with the same source directory, it will rerun the chunking process. 
 Dont forget to add the `--reuse` flag in order to reuse the stored chunk files if you dont want to rerun the chunking.

dsync can be pointed towards a cloud location by using the `--cloud` flag. 
 Your destination will need to follow the rclone convention of `configured-endpoint:bucket/path/path`
 This will launch dsync using rclone as the transfer tool. Please define your cloud endpoints in the rclone config file before running.
 You can also feed rclone a config file using `--rclone-config` otherwise it will default to `~/.config/rclone/rclone.conf`.

fpart can be skipped by using --no-fpart. This will perform a rudimentary chunking of the top level directories of the source path.

You can run rsync or clone in dry run mode using the flag `--dry-run`


If you run dsync with the `-h` flag you will get the usage:
```
uusage: dsync.py [-h] -n NUMBER [--no-fpart] [--source-hosts SOURCE_HOSTS]
                [--destination-hosts DESTINATION_HOSTS] [--reuse] [--cloud]
                [--dry-run] [--rclone-config RCLONE_CONFIG]
                [--working-dir /working/dir/] [--log-output /log/dir/]
                /source/path/ /destination/path/ OR
                cloud-prefix:bucket-name/path/in/bucket/

Uses fpart to bag up filesystems into defined chunks, then transfers them
using rsync/rclone either on a single host

positional arguments:
  /source/path/         Source path -- use absolute paths! (dsync always
                        behaves as if you used a trailing slash in rsync!)
  /destination/path/ OR cloud-prefix:bucket-name/path/in/bucket/
                        Destination path -- use absolute paths!

optional arguments:
  -h, --help            show this help message and exit
  -n NUMBER, --number NUMBER
                        Pack files into <num> chunks and kickoff <num>
                        transfers
  --no-fpart            Run without fpart in basic mode (Chunks consist of top
                        level files/dirs) WARNING: BROKEN WITH RCLONE, ONLY
                        TRANSFERS FILES!
  --source-hosts SOURCE_HOSTS
                        Provide a file with a list of hosts you want to run
                        the transfers to run on (will evenly balance out the #
                        of transfers with the number of hosts)
  --destination-hosts DESTINATION_HOSTS
                        Provide a file with a list of hosts you want the
                        transfers to run against (For example if you have a
                        number of remote hosts with an NFS storage mount)
  --reuse               Reuse existing chunk files from same source, and same
                        working directory
  --cloud               Upload data to a cloud provider using rclone instead
                        of local rsync
  --dry-run             Run rclone or rsync in dry run mode (Wont actually
                        copy anything)
  --rclone-config RCLONE_CONFIG
                        Path to config file for rclone (If not defined, will
                        default to ~/.config/rclone/rclone.conf)
  --working-dir /working/dir/
                        Directory in which temp files will be stored while
                        running (default is your home dir ~/dsync_working/)
  --log-output /log/dir/
                        location for the log files (Default is in the working
                        directory ~/dsync_working/logs/)
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details

## Acknowledgments

----