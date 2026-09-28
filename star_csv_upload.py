import csv
import hashlib
import json
import os
import re
import threading
import time
import tkinter as tk
import urllib.parse
from tkinter import filedialog, messagebox, scrolledtext

import requests


LOGIN_URL = "https://starsoftwaresolution.in/admin/login"
AUTH_URL = "https://starsoftwaresolution.in/login"
UPLOAD_PAGE_URL = "https://starsoftwaresolution.in/reports-upload"
IMPORT_URL = "https://starsoftwaresolution.in/reports/import"

STATE_FILE = "star_uploaded_csv_files.txt"
LOG_FILE = "star_csv_log.txt"
ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
EXPECTED_CSV_HEADERS = ["unique id", "name", "mobile", "report pdf"]


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


def normalize_path(path):
    return os.path.normcase(os.path.abspath(path))


def file_fingerprint(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = os.stat(file_path)
    return {
        "path": normalize_path(file_path),
        "sha256": digest.hexdigest(),
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
    }


def list_input_files(path_value):
    path_value = path_value.strip()
    if not path_value:
        return []

    if os.path.isfile(path_value):
        ext = os.path.splitext(path_value)[1].lower()
        return [path_value] if ext in ALLOWED_EXTENSIONS else []

    if os.path.isdir(path_value):
        found = []
        for root, _, files in os.walk(path_value):
            for name in files:
                if os.path.splitext(name)[1].lower() in ALLOWED_EXTENSIONS:
                    found.append(os.path.join(root, name))
        return sorted(found)

    return []


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

    payload = {
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
    response = session.post(AUTH_URL, data=payload, headers=headers, timeout=20)
    response.raise_for_status()

    if "admin/login" in response.url or "login" in response.url.lower():
        raise RuntimeError("Login failed. Please check Star email and password.")

    page = session.get(UPLOAD_PAGE_URL, timeout=20)
    page.raise_for_status()
    upload_token = extract_csrf_token(page.text) or csrf_token
    return session, upload_token


def refresh_upload_page(session):
    response = session.get(UPLOAD_PAGE_URL, timeout=20)
    response.raise_for_status()
    return extract_csrf_token(response.text)


def validate_source_file(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, "Unsupported file type."

    if ext == ".csv":
        try:
            with open(file_path, "r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
        except Exception as exc:
            return False, f"CSV read failed: {exc}"

        if not header:
            return False, "CSV file is empty."

        normalized = [str(item).strip().lower() for item in header[:4]]
        if normalized != EXPECTED_CSV_HEADERS:
            return False, (
                "CSV header mismatch. Expected: Unique Id, Name, Mobile, Report PDF"
            )

    return True, "OK"


def upload_csv_or_excel(session, file_path, laboratory_id, csrf_token):
    xsrf = urllib.parse.unquote(session.cookies.get("XSRF-TOKEN", ""))
    headers = {
        "X-XSRF-TOKEN": xsrf,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "Referer": UPLOAD_PAGE_URL,
        "Origin": "https://starsoftwaresolution.in",
    }

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".csv":
        mime = "text/csv"
    elif ext == ".xls":
        mime = "application/vnd.ms-excel"
    else:
        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    data = {
        "_token": csrf_token,
        "laboratory_id": laboratory_id,
    }
    with open(file_path, "rb") as handle:
        files = {"excel": (os.path.basename(file_path), handle, mime)}
        response = session.post(IMPORT_URL, data=data, files=files, headers=headers, timeout=60)
    return response


def is_already_uploaded(uploaded, fingerprint, laboratory_id):
    for item in uploaded:
        if item.get("laboratory_id") != laboratory_id:
            continue
        if item.get("sha256") == fingerprint["sha256"]:
            return True
        if (
            item.get("path") == fingerprint["path"]
            and item.get("size") == fingerprint["size"]
            and item.get("mtime") == fingerprint["mtime"]
        ):
            return True
    return False


def record_upload(uploaded, file_path, fingerprint, laboratory_id):
    uploaded.append(
        {
            "file_path": file_path,
            "path": fingerprint["path"],
            "sha256": fingerprint["sha256"],
            "size": fingerprint["size"],
            "mtime": fingerprint["mtime"],
            "laboratory_id": laboratory_id,
            "upload_type": "Star CSV",
            "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


def worker(source_path, email, password, laboratory_id, interval, run_once, log_box, stop_event):
    try:
        append_log(log_box, f"Logging in to Star Software as {email}...")
        session, csrf_token = star_login(email, password)
        append_log(log_box, "Login successful.")
    except Exception as exc:
        append_log(log_box, f"Login failed: {exc}")
        return

    uploaded = load_uploaded_files()
    append_log(log_box, f"Loaded {len(uploaded)} previously uploaded CSV/Excel entries.")

    cycle = 1
    while not stop_event.is_set():
        source_files = list_input_files(source_path)
        if not source_files:
            append_log(log_box, f"No CSV/Excel files found for {source_path}.")
        else:
            seen_hashes = set()
            new_uploads = 0

            for file_path in source_files:
                if stop_event.is_set():
                    append_log(log_box, "Stop requested. Ending Star CSV upload.")
                    save_uploaded_files(uploaded)
                    return

                valid, reason = validate_source_file(file_path)
                if not valid:
                    append_log(log_box, f"Skipping {os.path.basename(file_path)}: {reason}")
                    continue

                try:
                    fingerprint = file_fingerprint(file_path)
                except Exception as exc:
                    append_log(log_box, f"Skipping {os.path.basename(file_path)}: fingerprint failed - {exc}")
                    continue

                if fingerprint["sha256"] in seen_hashes:
                    append_log(log_box, f"Skipping duplicate content in current scan: {os.path.basename(file_path)}")
                    continue
                seen_hashes.add(fingerprint["sha256"])

                if is_already_uploaded(uploaded, fingerprint, laboratory_id):
                    append_log(log_box, f"Skipping already uploaded data: {os.path.basename(file_path)}")
                    continue

                try:
                    csrf_token = refresh_upload_page(session) or csrf_token
                    append_log(log_box, f"Uploading {os.path.basename(file_path)} for laboratory {laboratory_id}...")
                    response = upload_csv_or_excel(session, file_path, laboratory_id, csrf_token)
                except Exception as exc:
                    append_log(log_box, f"Upload error for {os.path.basename(file_path)}: {exc}")
                    continue

                payload = None
                try:
                    payload = response.json()
                except ValueError:
                    payload = None

                success = False
                if response.status_code in (200, 201):
                    if isinstance(payload, dict):
                        success = bool(payload.get("status")) or "/reports" in response.url
                    else:
                        success = "/reports" in response.url and "reports-upload" not in response.url
                elif response.status_code in (302, 303):
                    location = response.headers.get("Location", "")
                    success = "/reports" in location

                if success:
                    record_upload(uploaded, file_path, fingerprint, laboratory_id)
                    save_uploaded_files(uploaded)
                    new_uploads += 1
                    message = payload.get("message") if isinstance(payload, dict) else "Imported"
                    append_log(log_box, f"Success: {os.path.basename(file_path)} - {message}")
                elif response.status_code == 419:
                    append_log(log_box, f"Failed: CSRF token mismatch for {os.path.basename(file_path)}")
                elif response.status_code == 401:
                    append_log(log_box, "Failed: Session expired. Please start again.")
                    save_uploaded_files(uploaded)
                    return
                else:
                    if isinstance(payload, dict):
                        message = payload.get("message", str(payload))
                    else:
                        message = response.text[:250]
                    append_log(
                        log_box,
                        f"Failed: HTTP {response.status_code} for {os.path.basename(file_path)} - {message}",
                    )

                time.sleep(1)

            if new_uploads == 0:
                append_log(log_box, f"Cycle {cycle}: no new CSV/Excel files uploaded.")

        if run_once:
            append_log(log_box, "Run-once mode completed.")
            break

        cycle += 1
        append_log(log_box, f"Waiting {interval} seconds before next scan...")
        for _ in range(interval):
            if stop_event.is_set():
                append_log(log_box, "Stop requested. Ending Star CSV upload.")
                save_uploaded_files(uploaded)
                return
            time.sleep(1)

    save_uploaded_files(uploaded)


def browse_file(source_entry):
    selected = filedialog.askopenfilename(
        filetypes=[
            ("CSV / Excel files", "*.csv;*.xlsx;*.xls"),
            ("CSV files", "*.csv"),
            ("Excel files", "*.xlsx;*.xls"),
            ("All files", "*.*"),
        ]
    )
    if selected:
        source_entry.delete(0, "end")
        source_entry.insert(0, selected)


def browse_folder(source_entry):
    selected = filedialog.askdirectory()
    if selected:
        source_entry.delete(0, "end")
        source_entry.insert(0, selected)


def clear_uploaded_history(log_box):
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    append_log(log_box, "Cleared Star CSV upload history.")


def start_upload(source_entry, email_entry, password_entry, lab_entry, interval_entry, once_var, log_box, start_button, stop_button, stop_event):
    source_path = source_entry.get().strip()
    email = email_entry.get().strip()
    password = password_entry.get().strip()
    laboratory_id = lab_entry.get().strip() or "1"
    interval_value = interval_entry.get().strip() or "5"

    if not source_path or (not os.path.isfile(source_path) and not os.path.isdir(source_path)):
        messagebox.showerror("Error", "Please choose a valid file or folder.")
        return
    if not email or not password:
        messagebox.showerror("Error", "Star email and password are required.")
        return

    discovered = list_input_files(source_path)
    if not discovered:
        messagebox.showerror("Error", "No valid CSV/Excel files were found in the selected source.")
        return

    try:
        interval = int(interval_value)
    except ValueError:
        interval = 5

    stop_event.clear()
    start_button.configure(state="disabled")
    stop_button.configure(state="normal")
    append_log(log_box, f"Starting Star CSV uploader. Interval: {interval}s, Lab ID: {laboratory_id}")

    thread = threading.Thread(
        target=worker,
        args=(source_path, email, password, laboratory_id, interval, once_var.get(), log_box, stop_event),
        daemon=True,
    )
    thread.start()


def stop_upload(start_button, stop_button, stop_event, log_box):
    stop_event.set()
    start_button.configure(state="normal")
    stop_button.configure(state="disabled")
    append_log(log_box, "Stop requested.")


def build_gui():
    root = tk.Tk()
    root.title("Star CSV Upload")
    root.geometry("980x600")

    frame = tk.Frame(root, padx=10, pady=10)
    frame.pack(fill="both", expand=True)

    tk.Label(frame, text="CSV / Excel Source:").grid(row=0, column=0, sticky="w")
    source_entry = tk.Entry(frame, width=74)
    source_entry.grid(row=0, column=1, padx=4, pady=4, sticky="we")
    tk.Button(frame, text="Pick File", command=lambda: browse_file(source_entry)).grid(row=0, column=2, padx=4)
    tk.Button(frame, text="Pick Folder", command=lambda: browse_folder(source_entry)).grid(row=0, column=3, padx=4)

    tk.Label(frame, text="Star Email:").grid(row=1, column=0, sticky="w")
    email_entry = tk.Entry(frame, width=40)
    email_entry.insert(0, "admin@admin.com")
    email_entry.grid(row=1, column=1, padx=4, pady=4, sticky="w")

    tk.Label(frame, text="Star Password:").grid(row=2, column=0, sticky="w")
    password_entry = tk.Entry(frame, width=40, show="*")
    password_entry.insert(0, "123456789")
    password_entry.grid(row=2, column=1, padx=4, pady=4, sticky="w")

    tk.Label(frame, text="Laboratory ID:").grid(row=3, column=0, sticky="w")
    lab_entry = tk.Entry(frame, width=10)
    lab_entry.insert(0, "1")
    lab_entry.grid(row=3, column=1, padx=4, pady=4, sticky="w")

    tk.Label(frame, text="Interval (seconds):").grid(row=4, column=0, sticky="w")
    interval_entry = tk.Entry(frame, width=10)
    interval_entry.insert(0, "5")
    interval_entry.grid(row=4, column=1, padx=4, pady=4, sticky="w")

    once_var = tk.BooleanVar(value=False)
    tk.Checkbutton(frame, text="Run once", variable=once_var).grid(row=5, column=1, sticky="w")

    info = (
        "Supports one CSV/Excel file or a whole folder.\n"
        "Deduplication uses file content hash plus lab ID, so renamed copies do not re-upload."
    )
    tk.Label(frame, text=info, justify="left", fg="blue").grid(row=6, column=0, columnspan=4, sticky="w", pady=(4, 8))

    stop_event = threading.Event()

    log_box = scrolledtext.ScrolledText(frame, width=112, height=25, state="disabled")

    start_button = tk.Button(frame, text="Start Star CSV Upload", width=22)
    stop_button = tk.Button(frame, text="Stop", width=12, state="disabled")
    clear_button = tk.Button(frame, text="Clear Upload History", width=18, command=lambda: clear_uploaded_history(log_box))

    start_button.configure(
        command=lambda: start_upload(
            source_entry,
            email_entry,
            password_entry,
            lab_entry,
            interval_entry,
            once_var,
            log_box,
            start_button,
            stop_button,
            stop_event,
        )
    )
    stop_button.configure(command=lambda: stop_upload(start_button, stop_button, stop_event, log_box))

    start_button.grid(row=7, column=0, pady=6, sticky="w")
    stop_button.grid(row=7, column=1, pady=6, sticky="w")
    clear_button.grid(row=7, column=3, pady=6, sticky="e")

    tk.Label(frame, text="Log:").grid(row=8, column=0, sticky="nw")
    log_box.grid(row=8, column=1, columnspan=3, sticky="nsew")

    frame.grid_columnconfigure(1, weight=1)
    frame.grid_rowconfigure(8, weight=1)

    root.mainloop()


if __name__ == "__main__":
    build_gui()
