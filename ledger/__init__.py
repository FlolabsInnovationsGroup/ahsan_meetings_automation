"""What must outlive every job folder and every restart: the list of videos on
the YouTube channel, the journal of uploads in flight, the meetings the
autopilot is following and the Zoom accounts it watches.

All of it is small text files in storage_dir/state (JSON Lines, or JSON
rewritten atomically), mirrored to storage_dir/state-backup after every
change. Nothing cleans those folders: the retention sweep and
deploy/cleanup.sh only touch jobs/, videos/ and transcripts/.
"""
