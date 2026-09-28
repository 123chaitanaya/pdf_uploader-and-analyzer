import os
import re
import time
import json
import threading
import urllib.parse
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

import requests


LOGIN_URL = "https://starsoftwaresolution.in/admin/login"
AUTH_URL = "https://starsoftwaresolution.in/login"
UPLOAD_PAGE_URL = "https://starsoftwaresolution.in/reports-upload"
UPLOAD_POST_URL = "https://starsoftwaresolution.in/reports-upload"

STATE_FILE = "star_uploaded_files.txt"
LOG_FILE = "star_pdf_log.txt"
MAX_PDF_SIZE = 2 * 1024 * 1024


def append_log(log_box, message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"

    log_box.configure(state="normal")
    log_box.insert("end", line + "\n")
    log_box.yview("end")
    log_box.configure(state="disabled")

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def load_uploaded_files():
    if not os.path.exists(STATE_FILE):
        return []

    uploaded = []
    with open(STATE_FILE, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                uploaded.append(json.loads(line))
            except json.JSONDecodeError:
                uploaded.append({"file_path": line})
    return uploaded


def save_uploaded_files(uploaded):
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        for item in uploaded:
            handle.write(json.dumps(item) + "\n")


def find_pdfs(folder):
    pdfs = []
    for root, _, files in os.walk(folder):
        for name in files:
            if name.lower().endswith(".pdf"):
                pdfs.append(os.path.join(root, name))
    return sorted(pdfs)


def extract_csrf_token(html):
    patterns = [
        r'name="_token" value="([^"]+)"',
        r'<meta name="csrf-token" content="([^"]+)"',
        r'content="([^"]+)" name="csrf-token"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    return ""


def star_login(email, password):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    login_page = session.get(LOGIN_URL, timeout=20)
    login_page.raise_for_status()
    csrf_token = extract_csrf_token(login_page.text)

    data = {
        "_token": csrf_token,
        "email": email,
        "password": password,
        "login_as": "web",
        "remember": "on",
    }
    headers = {
        "Origin": "https://starsoftwaresolution.in",
        "Referer": LOGIN_URL,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    response = session.post(AUTH_URL, data=data, headers=headers, timeout=20)
    response.raise_for_status()

    if "admin/login" in response.url or "login" in response.url.lower():
        raise RuntimeError("Login failed. Please check Star email and password.")

    page = session.get(UPLOAD_PAGE_URL, timeout=20)
    page.raise_for_status()
    fresh_csrf = extract_csrf_token(page.text) or csrf_token
    return session, fresh_csrf


def refresh_upload_page(session):
    response = session.get(UPLOAD_PAGE_URL, timeout=20)
    response.raise_for_status()
    return extract_csrf_token(response.text)


def upload_pdf(session, file_path):
    xsrf = urllib.parse.unquote(session.cookies.get("XSRF-TOKEN", ""))
    headers = {
        "X-XSRF-TOKEN": xsrf,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": UPLOAD_PAGE_URL,
        "Origin": "https://starsoftwaresolution.in",
    }

    with open(file_path, "rb") as handle:
        files = {"pdf": (os.path.basename(file_path), handle, "application/pdf")}
        response = session.post(UPLOAD_POST_URL, files=files, headers=headers, timeout=40)
    return response


def worker(folder, email, password, interval, run_once, log_box, stop_event):
    try:
        append_log(log_box, f"Logging in to Star Software as {email}...")
        session, csrf_token = star_login(email, password)
        append_log(log_box, "Login successful.")
    except Exception as exc:
        append_log(log_box, f"Login failed: {exc}")
        return

    uploaded = load_uploaded_files()
    append_log(log_box, f"Loaded {len(uploaded)} previously uploaded PDF entries.")

    cycle = 1
    while not stop_event.is_set():
        pdfs = find_pdfs(folder)
        if not pdfs:
            append_log(log_box, f"No PDF files found in {folder}.")
        else:
            new_uploads = 0
            for file_path in pdfs:
                if stop_event.is_set():
                    append_log(log_box, "Stop requested. Ending Star upload.")
                    save_uploaded_files(uploaded)
                    return

                already_uploaded = any(item.get("file_path") == file_path for item in uploaded)
                if already_uploaded:
                    append_log(log_box, f"Skipping already uploaded file: {os.path.basename(file_path)}")
                    continue

                try:
                    size = os.path.getsize(file_path)
                except OSError as exc:
                    append_log(log_box, f"Cannot read file size for {file_path}: {exc}")
                    continue

                if size > MAX_PDF_SIZE:
                    append_log(
                        log_box,
                        f"Skipping {os.path.basename(file_path)} because it is larger than 2 MB.",
                    )
                    continue

                try:
                    csrf_token = refresh_upload_page(session) or csrf_token
                    append_log(log_box, f"Uploading {os.path.basename(file_path)}...")
                    response = upload_pdf(session, file_path)
                except Exception as exc:
                    append_log(log_box, f"Upload error for {os.path.basename(file_path)}: {exc}")
                    continue

                try:
                    payload = response.json()
                except ValueError:
                    payload = None

                if response.status_code in (200, 201) and payload and payload.get("status") is True:
                    append_log(
                        log_box,
                        f"Success: {os.path.basename(file_path)} - {payload.get('message', 'Uploaded')}",
                    )
                    uploaded.append(
                        {
                            "file_path": file_path,
                            "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "upload_type": "Star PDF",
                            "csrf_token_seen": csrf_token,
                        }
                    )
                    new_uploads += 1
                    save_uploaded_files(uploaded)
                elif response.status_code == 419:
                    append_log(log_box, f"Failed: CSRF token mismatch for {os.path.basename(file_path)}")
                elif response.status_code == 401:
                    append_log(log_box, "Failed: Session expired. Please start again.")
                    save_uploaded_files(uploaded)
                    return
                else:
                    message = payload.get("message") if isinstance(payload, dict) else response.text[:200]
                    append_log(
                        log_box,
                        f"Failed: HTTP {response.status_code} for {os.path.basename(file_path)} - {message}",
                    )

                time.sleep(1)

            if new_uploads == 0:
                append_log(log_box, f"Cycle {cycle}: no new PDFs uploaded.")

        if run_once:
            append_log(log_box, "Run-once mode completed.")
            break

        cycle += 1
        append_log(log_box, f"Waiting {interval} seconds before next scan...")
        for _ in range(interval):
            if stop_event.is_set():
                append_log(log_box, "Stop requested. Ending Star upload.")
                save_uploaded_files(uploaded)
                return
            time.sleep(1)

    save_uploaded_files(uploaded)


def start_upload(folder_entry, email_entry, password_entry, interval_entry, once_var, log_box, start_button, stop_button, stop_event):
    folder = folder_entry.get().strip()
    email = email_entry.get().strip()
    password = password_entry.get().strip()
    interval_value = interval_entry.get().strip() or "5"

    if not folder or not os.path.isdir(folder):
        messagebox.showerror("Error", "Please choose a valid PDF folder.")
        return
    if not email or not password:
        messagebox.showerror("Error", "Star email and password are required.")
        return

    try:
        interval = int(interval_value)
    except ValueError:
        interval = 5

    stop_event.clear()
    start_button.configure(state="disabled")
    stop_button.configure(state="normal")
    append_log(log_box, f"Starting Star PDF uploader. Interval: {interval}s")

    thread = threading.Thread(
        target=worker,
        args=(folder, email, password, interval, once_var.get(), log_box, stop_event),
        daemon=True,
    )
    thread.start()


def stop_upload(start_button, stop_button, stop_event, log_box):
    stop_event.set()
    start_button.configure(state="normal")
    stop_button.configure(state="disabled")
    append_log(log_box, "Stop requested.")


def browse_folder(folder_entry):
    selected = filedialog.askdirectory()
    if selected:
        folder_entry.delete(0, "end")
        folder_entry.insert(0, selected)


def clear_uploaded_history(log_box):
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    append_log(log_box, "Cleared Star uploaded file history.")


def build_gui():
    root = tk.Tk()
    root.title("Star PDF Upload")
    root.geometry("920x560")

    frame = tk.Frame(root, padx=10, pady=10)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="PDF Folder:").grid(row=0, column=0, sticky="w")
    folder_entry = tk.Entry(frame, width=70)
    folder_entry.grid(row=0, column=1, padx=4, pady=4, sticky="we")
    tk.Button(frame, text="Browse", command=lambda: browse_folder(folder_entry)).grid(row=0, column=2, padx=4)

    tk.Label(frame, text="Star Email:").grid(row=1, column=0, sticky="w")
    email_entry = tk.Entry(frame, width=40)
    email_entry.insert(0, "admin@admin.com")
    email_entry.grid(row=1, column=1, padx=4, pady=4, sticky="w")

    tk.Label(frame, text="Star Password:").grid(row=2, column=0, sticky="w")
    password_entry = tk.Entry(frame, width=40, show="*")
    password_entry.insert(0, "123456789")
    password_entry.grid(row=2, column=1, padx=4, pady=4, sticky="w")

    tk.Label(frame, text="Interval (seconds):").grid(row=3, column=0, sticky="w")
    interval_entry = tk.Entry(frame, width=10)
    interval_entry.insert(0, "5")
    interval_entry.grid(row=3, column=1, padx=4, pady=4, sticky="w")

    once_var = tk.BooleanVar(value=False)
    tk.Checkbutton(frame, text="Run once", variable=once_var).grid(row=4, column=1, sticky="w")

    info = (
        "This standalone tool uploads PDFs only to Star Software Solution.\n"
        "It follows the live site behavior and skips files above 2 MB."
    )
    tk.Label(frame, text=info, justify="left", fg="blue").grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 8))

    stop_event = threading.Event()

    start_button = tk.Button(frame, text="Start Star PDF Upload", width=22)
    stop_button = tk.Button(frame, text="Stop", width=12, state="disabled")
    clear_button = tk.Button(frame, text="Clear Upload History", width=18, command=lambda: clear_uploaded_history(log_box))

    start_button.configure(
        command=lambda: start_upload(
            folder_entry,
            email_entry,
            password_entry,
            interval_entry,
            once_var,
            log_box,
            start_button,
            stop_button,
            stop_event,
        )
    )
    stop_button.configure(command=lambda: stop_upload(start_button, stop_button, stop_event, log_box))

    start_button.grid(row=6, column=0, pady=6, sticky="w")
    stop_button.grid(row=6, column=1, pady=6, sticky="w")
    clear_button.grid(row=6, column=2, pady=6, sticky="e")

    tk.Label(frame, text="Log:").grid(row=7, column=0, sticky="nw")
    log_box = scrolledtext.ScrolledText(frame, width=110, height=24, state="disabled")
    log_box.grid(row=7, column=1, columnspan=2, sticky="nsew")

    frame.grid_columnconfigure(1, weight=1)
    frame.grid_rowconfigure(7, weight=1)

    root.mainloop()


if __name__ == "__main__":
    build_gui()
