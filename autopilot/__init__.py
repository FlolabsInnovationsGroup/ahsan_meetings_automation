"""The autopilot: every new Zoom recording of the watched accounts is
processed and posted to YouTube by itself, never twice.

- emails.py: the accounts in the page's dropdown, and which ones it watches.
- store.py: the meetings it follows, saved in storage_dir/state/autopilot.json.
- engine.py: one step of the work (find recordings, wait for Zoom's
  transcript, start the job, post the result), run by loop.py every minute.
- webhook.py: Zoom's "recording is ready" calls, which only wake it up.
- What may go up at all is the ledger's call (ledger/uploads.py).
"""
