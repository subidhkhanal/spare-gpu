import os

SECRET_KEY = os.environ["NOTESVC_SECRET"]  # signs session cookies
UPLOAD_DIR = os.environ.get("NOTESVC_UPLOADS", "uploads")
