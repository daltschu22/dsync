# dsync
PURPOSE:
This is a script to help move large amounts of data via multiple threads using either rsync or rclone. 

It works using fpart, a tool developed by Ganael LAPLANCHE (https://github.com/martymac/fpart) 
that bags up sections of the source directory into equal sizes. 

You can run against a local filesystem, or cloud destinations.

If running against a cloud provider, you need to have rclone. (https://rclone.org)
You will need an existing rclone configuration setup for the cloud provider. 
