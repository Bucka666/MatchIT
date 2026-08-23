"""
check_row_count.py — Quick check of a vertical's row count on the Modal
volume's images.db, without downloading the whole file.

Usage:
    modal run check_row_count.py
"""
from smart_upload import app, remote_get_row_count


@app.local_entrypoint()
def check_row_count():
    count = remote_get_row_count.remote("cards")
    print(f"[ROW-COUNT] cards vertical volume row count: {count}")
