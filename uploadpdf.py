import requests
import os
import json
import time
import threading
import base64
import csv
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timedelta
import re
import urllib.parse
import uuid

try:
    import pdfplumber
except Exception:
    pdfplumber = None

try:
    import openpyxl
    from openpyxl.styles import PatternFill
except Exception:
    openpyxl = None
    PatternFill = None

try:
    import speech_recognition as sr
except Exception:
    sr = None
try:
    import vosk
    # Force initialization to catch DLL errors now, not later
    vosk.Recognizer()
except Exception:
    vosk = None

try:
    import sounddevice as sd
    import numpy as np
    # Test that they work
    if sd is None or np is None:
        raise ImportError("Invalid")
except Exception:
    sd = None
    np = None

try:
    import whisper
except Exception:
    whisper = None

import queue
import tempfile
import wave

DESKTOP_CONFIG_FILE = 'desktop_api_config.json'
WALOCAL_AUTHKEY = "16abb1-7a7d66-dc5790-36a669-8bbf11"
WALOCAL_TEMPID = "patient_msg"
WALOCAL_SENDER = "7742684111"
DEFAULT_API_BASE_URL = os.environ.get('UPLOADPDF_API_BASE_URL', 'http://127.0.0.1:8000/api/v1')
DEFAULT_APP_LOGIN_USERNAME = os.environ.get('UPLOADPDF_APP_USERNAME', 'admin')
DEFAULT_APP_LOGIN_PASSWORD = os.environ.get('UPLOADPDF_APP_PASSWORD', 'admin123')
REQUEST_TIMEOUT = 20
APP_SESSION = {
    'api_base_url': DEFAULT_API_BASE_URL,
    'access_token': None,
    'user': None,
}
ACTIVE_BACKEND_JOB_IDS = set()


def normalize_api_base_url(raw_url):
    cleaned = raw_url.strip().rstrip('/')
    if not cleaned:
        raise ValueError('Server URL is required.')
    if cleaned.endswith('/api/v1'):
        return cleaned
    if cleaned.endswith('/api'):
        return cleaned + '/v1'
    if '/api/' in cleaned:
        return cleaned
    return cleaned + '/api/v1'


def load_desktop_api_config():
    config = {
        'api_base_url': DEFAULT_API_BASE_URL,
        'email': '',
        'abdm_client_id': '',
        'abdm_client_secret': '',
        'app_login_username': DEFAULT_APP_LOGIN_USERNAME,
        'app_login_password': DEFAULT_APP_LOGIN_PASSWORD,
    }
    if not os.path.exists(DESKTOP_CONFIG_FILE):
        return config

    try:
        with open(DESKTOP_CONFIG_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            config.update({
                'api_base_url': saved.get('api_base_url') or config['api_base_url'],
                'email': saved.get('email') or '',
                'abdm_client_id': saved.get('abdm_client_id') or '',
                'abdm_client_secret': saved.get('abdm_client_secret') or '',
                'app_login_username': saved.get('app_login_username') or config['app_login_username'],
                'app_login_password': saved.get('app_login_password') or config['app_login_password'],
            })
    except Exception:
        pass
    return config



def save_desktop_api_config(api_base_url, email):
    current = load_desktop_api_config()
    current.update({
        'api_base_url': api_base_url,
        'email': email.strip(),
    })
    try:
        with open(DESKTOP_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)
    except Exception:
        pass


def save_local_login_config(username, password):
    current = load_desktop_api_config()
    current.update({
        'app_login_username': username.strip(),
        'app_login_password': password,
    })
    try:
        with open(DESKTOP_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)
    except Exception:
        pass


def save_abdm_config(client_id, client_secret):
    current = load_desktop_api_config()
    current.update({
        'abdm_client_id': client_id.strip(),
        'abdm_client_secret': client_secret.strip(),
    })
    try:
        with open(DESKTOP_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)
    except Exception:
        pass



def set_authenticated_session(api_base_url, token_payload):
    APP_SESSION['api_base_url'] = api_base_url
    APP_SESSION['access_token'] = token_payload.get('access_token')
    APP_SESSION['user'] = token_payload.get('user') or {}


def get_authenticated_user_label():
    user = APP_SESSION.get('user') or {}
    full_name = (user.get('full_name') or '').strip()
    email = (user.get('email') or '').strip()
    return full_name or email or 'Unknown user'


def extract_response_error(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get('detail') or payload.get('message')
            if detail:
                return str(detail)
    except Exception:
        pass

    text = (response.text or '').strip()
    return text[:200] if text else f'HTTP {response.status_code}'


def api_request(method, path, use_auth=True, **kwargs):
    base_url = APP_SESSION.get('api_base_url')
    if not base_url:
        raise RuntimeError('Backend server URL is not configured.')

    headers = dict(kwargs.pop('headers', {}) or {})
    headers.setdefault('Accept', 'application/json')

    if use_auth:
        token = APP_SESSION.get('access_token')
        if token:
            headers['Authorization'] = f'Bearer {token}'

    return requests.request(method, f'{base_url}{path}', headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)


def authenticate_desktop_user(api_base_url, email, password):
    try:
        normalized_url = normalize_api_base_url(api_base_url)
    except ValueError as exc:
        return False, str(exc)

    try:
        response = requests.post(
            f'{normalized_url}/auth/token',
            json={
                'email': email.strip(),
                'password': password,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, f'Could not reach secure server: {exc}'

    if response.status_code != 200:
        return False, extract_response_error(response)

    token_payload = response.json()
    set_authenticated_session(normalized_url, token_payload)
    save_desktop_api_config(normalized_url, email)
    return True, ''


def build_job_items(file_paths, fallback_name, deduplicate=True):
    seen = set()
    items = []
    for path in file_paths or []:
        normalized = (path or '').strip()
        if not normalized:
            continue
        if deduplicate and normalized in seen:
            continue
        seen.add(normalized)
        items.append({
            'file_name': os.path.basename(normalized) or fallback_name,
            'file_path': normalized,
        })
        if len(items) >= 500:
            break

    if not items:
        items.append({
            'file_name': fallback_name,
            'file_path': None,
        })
    return items


def create_backend_job(channel, source_path='', source_file_name=None, file_paths=None, notes=None, lab_id=None, items=None):
    fallback_name = source_file_name or f'{channel}-session'
    serialized_notes = json.dumps(notes) if isinstance(notes, (dict, list)) else notes
    payload = {
        'channel': channel,
        'lab_id': lab_id,
        'source_path': source_path or None,
        'source_file_name': source_file_name,
        'notes': serialized_notes,
        'items': items if items is not None else build_job_items(file_paths or [], fallback_name),
    }
    response = api_request('POST', '/jobs', json=payload)
    if response.status_code != 201:
        raise RuntimeError(extract_response_error(response))
    return response.json()


def update_backend_job_status(job_id, status_value, message=None):
    payload = {'status': status_value}
    if message:
        payload['message'] = message
    response = api_request('PATCH', f'/jobs/{job_id}/status', json=payload)
    if response.status_code != 200:
        raise RuntimeError(extract_response_error(response))
    return response.json()


def fetch_backend_job(job_id):
    response = api_request('GET', f'/jobs/{job_id}')
    if response.status_code != 200:
        raise RuntimeError(extract_response_error(response))
    return response.json()


def cancel_backend_job(job_id, log_box=None):
    try:
        update_backend_job_status(job_id, 'cancelled', 'Cancelled by desktop user.')
    except Exception as exc:
        if log_box is not None:
            log(f'Warning: backend job {job_id} could not be cancelled: {exc}', log_box)


def cancel_active_backend_jobs(log_box=None):
    for job_id in list(ACTIVE_BACKEND_JOB_IDS):
        cancel_backend_job(job_id, log_box=log_box)


def monitor_backend_job(job_id, channel_label, log_box, on_complete=None, stop_event=None):
    seen_log_ids = set()
    try:
        while True:
            job = fetch_backend_job(job_id)
            for entry in job.get('logs', []):
                log_id = entry.get('id')
                if log_id in seen_log_ids:
                    continue
                seen_log_ids.add(log_id)
                log(f"[Job #{job_id}] {entry.get('message', '')}", log_box)

            status_value = job.get('status')
            if status_value in ('completed', 'failed', 'cancelled'):
                if status_value == 'completed':
                    log(f'{channel_label} finished on secure server.', log_box)
                elif status_value == 'cancelled':
                    log(f'{channel_label} was cancelled on secure server.', log_box)
                else:
                    log(f'{channel_label} failed on secure server.', log_box)
                break

            time.sleep(2)
    except Exception as exc:
        log(f'Warning: lost backend job monitor for {channel_label}: {exc}', log_box)
    finally:
        ACTIVE_BACKEND_JOB_IDS.discard(job_id)
        if on_complete is not None:
            try:
                on_complete()
            except Exception:
                pass


def start_backend_tracked_worker(
    channel,
    channel_label,
    log_box,
    stop_event,
    worker_func=None,
    worker_args=None,
    source_path='',
    source_file_name=None,
    file_paths=None,
    notes=None,
    lab_id=None,
    items=None,
    on_complete=None,
):
    job = create_backend_job(
        channel=channel,
        source_path=source_path,
        source_file_name=source_file_name,
        file_paths=file_paths or [],
        notes=notes,
        lab_id=lab_id,
        items=items,
    )
    job_id = job.get('id')
    ACTIVE_BACKEND_JOB_IDS.add(job_id)
    log(f'Created backend job #{job_id} for {channel_label}.', log_box)

    thread = threading.Thread(
        target=monitor_backend_job,
        args=(job_id, channel_label, log_box, on_complete, stop_event),
        daemon=True,
    )
    thread.start()
    return job_id, thread


def collect_files_by_extensions(folder, extensions):
    matches = []
    if folder and os.path.isdir(folder):
        for root, dirs, files in os.walk(folder):
            for file_name in files:
                if file_name.lower().endswith(extensions):
                    matches.append(os.path.join(root, file_name))
    return matches


def collect_star_job_files(folder, csv_file):
    all_files = collect_files_by_extensions(folder, ('.pdf', '.csv', '.xlsx', '.xls'))

    if csv_file:
        if os.path.isdir(csv_file):
            for root, dirs, files in os.walk(csv_file):
                for file_name in files:
                    if file_name.lower().endswith(('.csv', '.xlsx', '.xls')):
                        full_path = os.path.join(root, file_name)
                        if full_path not in all_files:
                            all_files.append(full_path)
        elif os.path.isfile(csv_file) and csv_file not in all_files:
            all_files.append(csv_file)

    return sorted(all_files, key=lambda p: (0 if p.lower().endswith('.pdf') else 1, os.path.basename(p).lower()))


def collect_csv_attachment_paths(csv_file, folder):
    attachments = []
    if not csv_file or not os.path.isfile(csv_file):
        return attachments

    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return attachments

    for line in lines:
        values = line.strip('\n').split(',')
        if len(values) >= 3:
            attachment_name = values[2].strip()
            if attachment_name:
                attachments.append(os.path.join(folder, attachment_name))
    return attachments


def build_csv_batch_job_items(csv_file, folder):
    items = []
    if not csv_file or not os.path.isfile(csv_file):
        return [{'file_name': os.path.basename(csv_file) or 'csv-batch', 'file_path': csv_file or None}]

    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception:
        return [{'file_name': os.path.basename(csv_file), 'file_path': csv_file}]

    for index, line in enumerate(lines, start=1):
        values = line.strip('\n').split(',')
        attachment_name = values[2].strip() if len(values) >= 3 else ''
        file_path = os.path.join(folder, attachment_name) if folder and attachment_name else None
        display_name = attachment_name or f'row-{index}'
        items.append({
            'file_name': f'row-{index}-{display_name}',
            'file_path': file_path,
        })
        if len(items) >= 500:
            break

    if not items:
        items.append({'file_name': os.path.basename(csv_file), 'file_path': csv_file})
    return items


def draw_star_logo(parent):
    canvas = tk.Canvas(parent, width=150, height=130, bg='white', highlightthickness=0)
    points = [
        75, 8,
        90, 52,
        138, 52,
        99, 79,
        114, 122,
        75, 95,
        36, 122,
        51, 79,
        12, 52,
        60, 52,
    ]
    canvas.create_polygon(points, fill='white', outline='#11a2ea', width=4, smooth=True)
    canvas.create_line(103, 36, 142, 36, fill='#f28c28', width=4)
    return canvas


def create_brand_header(parent):
    header = tk.Frame(parent, bg='white', bd=1, relief='solid', padx=10, pady=8)

    logo_holder = tk.Frame(header, bg='white')
    logo_holder.grid(row=0, column=0, rowspan=2, sticky='nw', padx=(0, 10))
    draw_star_logo(logo_holder).pack()

    text_holder = tk.Frame(header, bg='white')
    text_holder.grid(row=0, column=1, sticky='nw')

    tk.Label(
        text_holder,
        text='STAR SOFTWARE SOLUTION',
        font=('Times New Roman', 24, 'bold'),
        fg='#403b97',
        bg='white'
    ).grid(row=0, column=0, sticky='w')

    tk.Label(
        text_holder,
        text='Office : 87, Subhash Colony, Bhagat ki Kothi, Jodhpur. (Raj.)',
        font=('Arial', 12),
        fg='#333333',
        bg='white'
    ).grid(row=1, column=0, sticky='w')

    tk.Label(
        text_holder,
        text='Branch Office : RIICO Estate  Spl-"A", Near Shastri Nagar, Jodhpur',
        font=('Arial', 12),
        fg='#333333',
        bg='white'
    ).grid(row=2, column=0, sticky='w')

    tk.Label(
        text_holder,
        text='Mail : usbjodhpur@gmail.com',
        font=('Arial', 12),
        fg='#333333',
        bg='white'
    ).grid(row=3, column=0, sticky='w')

    tk.Frame(header, bg='#5a57ad', height=3).grid(row=1, column=1, sticky='ew', pady=(8, 0))
    header.grid_columnconfigure(1, weight=1)

    phone_box = tk.Frame(header, bg='white', bd=1, relief='ridge', padx=16, pady=10)
    phone_box.grid(row=0, column=2, rowspan=2, sticky='ne', padx=(12, 0))
    tk.Label(
        phone_box,
        text='(M) 94141-26496\n9314724006',
        font=('Arial', 14, 'bold'),
        fg='#444444',
        bg='white',
        justify='center'
    ).pack()

    return header


def show_app_login():
    saved_config = load_desktop_api_config()
    login_root = tk.Tk()
    login_root.title('UploadPDF Login')
    login_root.geometry('560x500')
    login_root.configure(bg='#f3f6fb')
    login_root.resizable(False, False)
    login_root.eval('tk::PlaceWindow . center')

    result = {'ok': False}

    shell = tk.Frame(login_root, bg='#f3f6fb', padx=28, pady=24)
    shell.pack(fill='both', expand=True)

    card = tk.Frame(shell, bg='white', padx=28, pady=24, bd=1, relief='solid')
    card.pack(fill='both', expand=True)

    draw_star_logo(card).pack(pady=(0, 4))
    tk.Label(card, text='UPLOADPDF', font=('Times New Roman', 24, 'bold'), fg='#403b97', bg='white').pack()
    tk.Label(card, text='Local Login', font=('Arial', 12), fg='#555555', bg='white').pack(pady=(2, 12))

    form = tk.Frame(card, bg='white')
    form.pack(fill='x')
    form.grid_columnconfigure(0, weight=1)

    tk.Label(form, text='Username / Email', bg='white', anchor='w', font=('Arial', 11, 'bold')).grid(row=0, column=0, sticky='w', pady=(0, 6))
    login_entry = tk.Entry(form, width=38, font=('Arial', 11))
    login_entry.insert(0, saved_config.get('app_login_username', DEFAULT_APP_LOGIN_USERNAME))
    login_entry.grid(row=1, column=0, sticky='ew', pady=(0, 10))

    tk.Label(form, text='Password', bg='white', anchor='w', font=('Arial', 11, 'bold')).grid(row=2, column=0, sticky='w', pady=(0, 6))
    password_entry = tk.Entry(form, width=38, show='*', font=('Arial', 11))
    password_entry.grid(row=3, column=0, sticky='ew', pady=(0, 10))

    status_label = tk.Label(card, text='', fg='red', bg='white', font=('Arial', 10), wraplength=470, justify='left')
    status_label.pack(pady=(10, 4), anchor='w')

    def attempt_login(event=None):
        username = login_entry.get().strip()
        password = password_entry.get()

        if not username or not password:
            status_label.config(text='Username/email and password are required.')
            return

        expected_username = (saved_config.get('app_login_username') or DEFAULT_APP_LOGIN_USERNAME).strip()
        expected_password = (saved_config.get('app_login_password') or DEFAULT_APP_LOGIN_PASSWORD)

        if username == expected_username and password == expected_password:
            APP_SESSION['api_base_url'] = saved_config.get('api_base_url', DEFAULT_API_BASE_URL)
            APP_SESSION['access_token'] = None
            APP_SESSION['user'] = {
                'full_name': username,
                'email': username,
            }
            save_local_login_config(username, password)
            result['ok'] = True
            login_root.destroy()
            return

        status_label.config(text='Invalid username/email or password.', fg='red')
        password_entry.delete(0, 'end')

    def cancel_login():
        login_root.destroy()

    btn_row = tk.Frame(card, bg='white')
    btn_row.pack(pady=(12, 0), anchor='e')
    tk.Button(btn_row, text='Login', width=14, bg='#403b97', fg='white', command=attempt_login).grid(row=0, column=0, padx=6)
    tk.Button(btn_row, text='Exit', width=14, command=cancel_login).grid(row=0, column=1, padx=6)

    login_entry.bind('<Return>', attempt_login)
    password_entry.bind('<Return>', attempt_login)
    if saved_config.get('app_login_username'):
        password_entry.focus_set()
    else:
        login_entry.focus_set()

    login_root.mainloop()
    return result['ok']


def upload_pdf(file_path, hospital_name, password, key_value_pairs):
    """Upload a single PDF file to the API."""
    url = "https://backend.nimantran.app/functions/v1/make-server-c25875c1/abdm/upload"
    with open(file_path, 'rb') as f:
        files = {'file': f}
        data = {
            'hospital_name': hospital_name,
            'password': password,
            'key_value_pairs': json.dumps(key_value_pairs)
        }
        response = requests.post(url, files=files, data=data)
        return response


def upload_file_to_hosting(file_path):
    """Host local files on catbox.moe to get a URL for WhatsApp/other APIs."""
    try:
        # Catbox is generally more reliable for this type of workflow
        data = {'reqtype': 'fileupload'}
        with open(file_path, 'rb') as f:
            files = {'fileToUpload': f}
            response = requests.post('https://catbox.moe/user/api.php', data=data, files=files, timeout=20)
            if response.status_code == 200:
                link = response.text.strip()
                if link.startswith('http'):
                    return True, link
                return False, f"Unexpected response: {link[:50]}"
            return False, f"HTTP {response.status_code}: {response.text[:100]}"
    except Exception as e:
        return False, f"Network/File error: {str(e)}"
    return False, "Unknown error"


def send_whatsapp_api(authkey, tempid, mobile, ph1, sender=None):
    """Send WhatsApp message via Walocal API."""
    base_url = "https://walocal.com/api/v23.0/"
    params = {
        'authkey': authkey,
        'tempid': tempid,
        'mobile': mobile,
        'ph1': ph1
    }
    if sender:
        params['sender'] = sender
    
    # Use requests.get as specified in the target URL structure
    try:
        response = requests.get(base_url, params=params, timeout=20)
        return response
    except Exception as e:
        # Return a mock-like response object for error handling consistency
        class ErrorResponse:
            def __init__(self, text, status_code):
                self.text = text
                self.status_code = status_code
        return ErrorResponse(str(e), 500)


# --- ABDM SANDBOX MOCK FUNCTIONS ---

def abdm_mock_generate_otp(identifier, log_box):
    """Simulate ABDM OTP generation."""
    val = identifier.get().strip()
    if not val:
        log("❌ ABDM Error: Mobile/Aadhaar number required.", log_box)
        return
    log(f"🔗 [MOCK] Sending ABDM OTP request for: {val}...", log_box)
    time.sleep(1)
    log(f"✅ [MOCK] OTP sent successfully to {val}. (Hint: Use '1234' for test)", log_box)


def abdm_mock_verify_otp(identifier, otp_entry, log_box):
    """Simulate ABDM OTP verification and ABHA creation."""
    ident = identifier.get().strip()
    otp = otp_entry.get().strip()
    if not ident or not otp:
        log("❌ ABDM Error: Identifier and OTP are both required.", log_box)
        return
    
    log(f"🔗 [MOCK] Verifying OTP {otp} for {ident}...", log_box)
    time.sleep(1.5)
    
    if otp == "1234":
        mock_abha = f"91-{ident[:4]}-{ident[-4:]}-2026"
        log(f"🎉 [MOCK] ABHA CREATED SUCCESSFULLY!", log_box)
        log(f"📌 ABHA ID: {mock_abha}", log_box)
        log(f"📌 Name: Mock Patient", log_box)
        messagebox.showinfo("ABDM Success", f"Mock ABHA Created: {mock_abha}")
    else:
        log("❌ ABDM Error: Invalid OTP provided.", log_box)
        messagebox.showerror("ABDM Error", "Invalid OTP. Use '1234' for simulation.")


def abdm_handle_save_settings(id_entry, secret_entry, log_box):
    """Save ABDM credentials to config."""
    client_id = id_entry.get().strip()
    secret = secret_entry.get().strip()
    save_abdm_config(client_id, secret)
    log("💾 ABDM Settings saved to config folder.", log_box)
    messagebox.showinfo("Settings Saved", "ABDM Client credentials have been saved.")




def read_b64(path):
    """Read a file and return its base64-encoded contents."""
    with open(path, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def invoke_nvidia_chat_completion(
    messages,
    model="google/gemma-4-31b-it",
    api_key=None,
    stream=True,
    max_tokens=16384,
    temperature=1.0,
    top_p=0.95,
    enable_thinking=True,
    timeout=60,
):
    """Invoke the NVIDIA Integrate chat completions endpoint.

    If stream=True, this yields event-stream lines as they arrive.
    Otherwise, it returns the parsed JSON response.
    """
    if api_key is None:
        api_key = os.environ.get('NVIDIA_API_KEY')
    if not api_key:
        raise RuntimeError('NVIDIA_API_KEY environment variable is required.')

    invoke_url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Accept': 'text/event-stream' if stream else 'application/json',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'top_p': top_p,
        'stream': stream,
        'chat_template_kwargs': {'enable_thinking': enable_thinking},
    }

    response = requests.post(invoke_url, headers=headers, json=payload, timeout=timeout, stream=stream)
    if stream:
        for line in response.iter_lines():
            if line:
                try:
                    yield line.decode('utf-8')
                except Exception:
                    yield line.decode('utf-8', errors='replace')
    else:
        return response.json()


def send_pdf_email(file_path, sender_email, sender_password, recipient_email, subject=None, body=None):
    """Send PDF as email attachment via Gmail SMTP."""
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = recipient_email
    msg['Subject'] = subject if subject else f"PDF Upload: {os.path.basename(file_path)}"

    body_text = body if body else f"Attached is the PDF file: {os.path.basename(file_path)}"
    msg.attach(MIMEText(body_text, 'plain'))

    with open(file_path, 'rb') as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(file_path)}"')
        msg.attach(part)

    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login(sender_email, sender_password)
    text = msg.as_string()
    server.sendmail(sender_email, recipient_email, text)
    server.quit()


def find_pdfs(folder):
    """Recursively find all PDF files in the given folder."""
    pdfs = []
    for root, dirs, files in os.walk(folder):
        for file in files:
            if file.lower().endswith('.pdf'):
                pdfs.append(os.path.join(root, file))
    return pdfs


def load_uploaded_files():
    """Load the set of uploaded files from a text file with hospital and path info."""
    uploaded_file = 'uploaded_files.txt'
    if os.path.exists(uploaded_file):
        with open(uploaded_file, 'r') as f:
            entries = []
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        entries.append(entry)
                    except json.JSONDecodeError:
                        # Handle legacy format (just file path)
                        entries.append({'file_path': line, 'hospital_name': 'Unknown', 'upload_path': 'Unknown'})
            return entries
    return []


def save_uploaded_files(uploaded_list):
    """Save the set of uploaded files with hospital and path info to a text file."""
    uploaded_file = 'uploaded_files.txt'
    with open(uploaded_file, 'w') as f:     
        for entry in uploaded_list:
            f.write(json.dumps(entry) + '\n')


def log(msg, log_box, log_file='application_log.txt'):
    """Log a message to the GUI and also to a specified text file."""
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    formatted_msg = f"[{timestamp}] {msg}"
    
    # 1. Update GUI
    log_box.configure(state='normal')
    log_box.insert('end', formatted_msg + "\n")
    log_box.yview('end')
    log_box.configure(state='disabled')
    
    # 2. Save to specific file
    try:
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(formatted_msg + "\n")
    except Exception:
        pass # Don't crash the app if log file is busy


class ReportExtractor:
    """Class to handle robust PDF parsing and data extraction."""

    def __init__(self):
        self.patterns = {
            'uhid': [
                r'(?i)\bUHID\s*[:\-]\s*(\w+[\/\w]*)',
                r'(?i)\bID\s*[:\-]\s*(\w+[\/\w]*)',
                r'(?i)\bReg\.?\s*No\.?\s*[:\-]\s*(\w+[\/\w]*)',
                r'(?i)\bPatient\s*ID\s*[:\-]\s*(\w+[\/\w]*)',
                r'(?i)\bUnique\s*Id\s*[:\-]\s*(\w+[\/\w]*)',
            ],
            'name': [
                r'(?i)\bPt\.?\s*Name\s*[:\-]\s*(.*)',
                r'(?i)\bPatient\s*Name\s*[:\-]\s*(.*)',
                r'(?i)\bName\s*[:\-]\s*(.*)',
            ],
            'mobile': [
                r'(?i)\bMob\.?\s*#?\s*[:\-]?\s*([\d\+\-\s]{7,15})',
                r'(?i)\bMobile\s*[:\-]\s*([\d\+\-\s]{7,15})',
                r'(?i)\bPhone\s*[:\-]\s*([\d\+\-\s]{7,15})',
                r'(?i)\bContact\s*[:\-]\s*([\d\+\-\s]{7,15})',
            ],
            'age': [
                r'(?i)\bAGE\s*[:\-]\s*(\d+)',
                r'(?i)\bAge\s*[:\-]\s*(\d+)',
                r'(?i)\b(\d+)\s*(?:Years|Yrs|Y|M|Months|Age)\b',
            ],
            'hb': [
                r'(?i)\bHemoglobin\b.*?(\d+\.?\d*)',
                r'(?i)\bHb\b.*?(\d+\.?\d*)',
            ],
            'wbc': [
                r'(?i)\b(?:WBC|Leucocyte|TLC)\b.*?(\d+\.?\d*)',
            ],
            'platelets': [
                r'(?i)\b(?:Platelet|PLT)\b.*?(\d+\.?\d*)',
            ],
        }

    def extract(self, pdf_path):
        if pdfplumber is None:
            raise Exception('pdfplumber is not installed. PDF analysis is unavailable.')

        data = {
            'uhid': None,
            'name': None,
            'mobile': None,
            'age': None,
            'hb': None,
            'wbc': None,
            'platelets': None,
        }

        try:
            with pdfplumber.open(pdf_path) as pdf:
                full_text = ''
                for page in pdf.pages:
                    page_text = page.extract_text() or ''
                    full_text += page_text + '\n'

                for key, patterns in self.patterns.items():
                    for pattern in patterns:
                        match = re.search(pattern, full_text)
                        if match:
                            val = match.group(1).strip()

                            if key == 'name':
                                val = re.split(r'(?i)Age|Sex|Date|Ref|Mob', val)[0].strip()

                            if key == 'age':
                                age_match = re.search(r'(\d+)', val)
                                if age_match:
                                    val = age_match.group(1)

                            if key == 'mobile':
                                val = val.replace(' ', '').strip()

                            data[key] = val
                            break
            return data
        except Exception as e:
            raise Exception(f'Error reading PDF: {str(e)}')


class DatabaseHandler:
    """Class to handle Excel (.xlsx) and CSV (.csv) database operations."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.headers = ['Unique Id', 'Patient Name', 'Mobile', 'Report PDF', 'Date']
        self.data_columns_map = {
            'uhid': 0,
            'name': 1,
            'mobile': 2,
        }

    def update_record(self, patient_data, pdf_filename, overwrite=False):
        ext = os.path.splitext(self.db_path)[1].lower()
        if ext == '.csv':
            return self._update_csv(patient_data, pdf_filename, overwrite)
        return self._update_xlsx(patient_data, pdf_filename, overwrite)

    def _update_csv(self, data, pdf_filename, overwrite):
        try:
            rows = []
            file_exists = os.path.exists(self.db_path)

            if file_exists:
                with open(self.db_path, 'r', newline='', encoding='utf-8') as f:
                    reader = csv.reader(f)
                    rows = list(reader)

            if not rows:
                rows = [self.headers]

            uhid = str(data.get('uhid') or '')
            if not uhid:
                raise Exception('Missing Unique Id / UHID in patient data.')

            uhid_col = 0
            for i, h in enumerate(rows[0]):
                h_upper = h.upper().strip()
                if 'UNIQUE' in h_upper or 'UHID' in h_upper:
                    uhid_col = i
                    break

            target_idx = -1
            for i in range(1, len(rows)):
                if len(rows[i]) > uhid_col and str(rows[i][uhid_col]).strip() == uhid:
                    target_idx = i
                    break

            current_date = datetime.now().strftime('%d/%m/%Y')

            if target_idx != -1:
                updated = False
                for key, col_idx in self.data_columns_map.items():
                    while len(rows[target_idx]) <= col_idx:
                        rows[target_idx].append('')

                    current_val = rows[target_idx][col_idx]
                    new_val = str(data.get(key) or '')

                    if new_val:
                        if overwrite or not current_val.strip():
                            rows[target_idx][col_idx] = new_val
                            updated = True

                while len(rows[target_idx]) <= 4:
                    rows[target_idx].append('')
                rows[target_idx][3] = pdf_filename
                rows[target_idx][4] = current_date
                updated = True

                action = 'Updated existing record (CSV)' if updated else 'No changes made (already filled)'
            else:
                num_cols = max(len(self.headers), len(rows[0]))
                new_row = [''] * num_cols
                for key, col_idx in self.data_columns_map.items():
                    if col_idx < num_cols:
                        new_row[col_idx] = str(data.get(key) or '')
                if 3 < num_cols:
                    new_row[3] = pdf_filename
                if 4 < num_cols:
                    new_row[4] = current_date
                rows.append(new_row)
                action = 'Added new record (CSV)'

            with open(self.db_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerows(rows)

            return action
        except Exception as e:
            raise Exception(f'CSV Error: {str(e)}')

    def _update_xlsx(self, patient_data, pdf_filename, overwrite=False):
        try:
            if openpyxl is None or PatternFill is None:
                raise Exception('openpyxl is not installed. XLSX database updates are unavailable.')

            if not os.path.exists(self.db_path):
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.append(self.headers)
                wb.save(self.db_path)

            wb = openpyxl.load_workbook(self.db_path)
            ws = wb.active

            uhid = str(patient_data.get('uhid') or '')
            if not uhid:
                raise Exception('Missing Unique Id / UHID in patient data.')

            uhid_col = 1
            for cell in ws[1]:
                if cell.value:
                    h_upper = str(cell.value).upper().strip()
                    if 'UNIQUE' in h_upper or 'UHID' in h_upper:
                        uhid_col = cell.column
                        break

            target_row = None
            for row in range(2, ws.max_row + 1):
                cell_val = ws.cell(row=row, column=uhid_col).value
                if cell_val is not None and str(cell_val).strip() == uhid:
                    target_row = row
                    break

            highlight_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')
            current_date = datetime.now().strftime('%d/%m/%Y')
            xlsx_data_map = {
                'uhid': 1,
                'name': 2,
                'mobile': 3,
            }

            if target_row:
                updated = False
                for key, col in xlsx_data_map.items():
                    current_val = ws.cell(row=target_row, column=col).value
                    new_val = patient_data.get(key)

                    if new_val:
                        if overwrite or current_val is None or str(current_val).strip() == '':
                            ws.cell(row=target_row, column=col).value = new_val
                            ws.cell(row=target_row, column=col).fill = highlight_fill
                            updated = True

                ws.cell(row=target_row, column=4).value = pdf_filename
                ws.cell(row=target_row, column=4).fill = highlight_fill
                ws.cell(row=target_row, column=5).value = current_date
                ws.cell(row=target_row, column=5).fill = highlight_fill
                updated = True

                action = 'Updated existing record (Excel)' if updated else 'No changes made (already filled)'
            else:
                new_row = ws.max_row + 1
                for key, col in xlsx_data_map.items():
                    val = patient_data.get(key)
                    ws.cell(row=new_row, column=col).value = val
                    ws.cell(row=new_row, column=col).fill = highlight_fill

                ws.cell(row=new_row, column=4).value = pdf_filename
                ws.cell(row=new_row, column=4).fill = highlight_fill
                ws.cell(row=new_row, column=5).value = current_date
                ws.cell(row=new_row, column=5).fill = highlight_fill
                action = 'Added new record (Excel)'

            wb.save(self.db_path)
            return action
        except Exception as e:
            raise Exception(f'Excel Error: {str(e)}')


def worker(folder, hospital_name, password, kvp, interval, once, log_box, stop_event):
    if interval <= 0:
        log("Interval must be a positive number.", log_box, log_file='api_log.txt')
        return

    uploaded_list = load_uploaded_files()
    log(f"Loaded {len(uploaded_list)} previously uploaded files.", log_box, log_file='api_log.txt')
    
    cycle = 1
    while not stop_event.is_set():
        pdfs = find_pdfs(folder)
        if not pdfs:
            log(f"No PDF files found in folder {folder}.", log_box, log_file='api_log.txt')
        else:
            new_uploads = 0
            for pdf in pdfs:
                if stop_event.is_set():
                    log("Upload stopped by user.", log_box, log_file='api_log.txt')
                    save_uploaded_files(uploaded_list)
                    return
                # Check if file already uploaded to the SAME hospital
                already_uploaded = any(entry['file_path'] == pdf and entry['hospital_name'] == hospital_name for entry in uploaded_list)
                if already_uploaded:
                    # On 2nd cycle and onwards, ask user if they want to re-upload
                    if cycle >= 2:
                        response = messagebox.askyesno("File Already Uploaded", 
                            f"File already uploaded to {hospital_name}:\n{pdf}\n\nDo you want to upload it again?")
                        if not response:
                            log(f"Skipping already uploaded: {pdf}", log_box, log_file='api_log.txt')
                            continue
                    else:
                        log(f"Skipping already uploaded: {pdf}", log_box, log_file='api_log.txt')
                        continue
                log(f"Uploading {pdf}... [Hospital: {hospital_name}, Path: {folder}]", log_box, log_file='api_log.txt')
                try:
                    response = upload_pdf(pdf, hospital_name, password, kvp)
                    if response.status_code == 200:
                        entry = {
                            'file_path': pdf,
                            'hospital_name': hospital_name,
                            'upload_path': folder,
                            'upload_timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                        }
                        uploaded_list.append(entry)
                        new_uploads += 1
                        log(f"Success: {response.status_code} - {response.text}", log_box, log_file='api_log.txt')
                    else:
                        log(f"Failed: {response.status_code} - {response.text}", log_box, log_file='api_log.txt')
                except Exception as ex:
                    log(f"Upload failed for {pdf}: {ex}", log_box, log_file='api_log.txt')
                time.sleep(2)

            if new_uploads > 0:
                save_uploaded_files(uploaded_list)
                log(f"Saved {len(uploaded_list)} uploaded files to disk.", log_box, log_file='api_log.txt')

        if once:
            log("Run-once mode completed.", log_box, log_file='api_log.txt')
            break
        
        cycle += 1

        log(f"Waiting {interval} seconds until next cycle...", log_box, log_file='api_log.txt')
        for _ in range(interval):
            if stop_event.is_set():
                log("Upload stopped by user.", log_box, log_file='api_log.txt')
                save_uploaded_files(uploaded_list)
                return
            time.sleep(1)

    save_uploaded_files(uploaded_list)


def gmail_worker(folder, gmail_sender, gmail_pwd, recipient, interval, once, log_box, stop_event):
    if interval <= 0:
        log("Interval must be a positive number.", log_box, log_file='gmail_log.txt')
        return

    uploaded_list = load_uploaded_files()
    log(f"Loaded {len(uploaded_list)} previously uploaded files.", log_box, log_file='gmail_log.txt')
    
    cycle = 1
    while not stop_event.is_set():
        pdfs = find_pdfs(folder)
        if not pdfs:
            log(f"No PDF files found in folder {folder}.", log_box, log_file='gmail_log.txt')
        else:
            new_uploads = 0
            for pdf in pdfs:
                if stop_event.is_set():
                    log("Upload stopped by user.", log_box, log_file='gmail_log.txt')
                    save_uploaded_files(uploaded_list)
                    return
                # Check if file already uploaded
                already_uploaded = any(entry['file_path'] == pdf for entry in uploaded_list)
                if already_uploaded:
                    # On 2nd cycle and onwards, ask user if they want to re-upload
                    if cycle >= 2:
                        response = messagebox.askyesno("File Already Uploaded", 
                            f"File already emailed:\n{pdf}\n\nDo you want to send it again?")
                        if not response:
                            log(f"Skipping already emailed: {pdf}", log_box, log_file='gmail_log.txt')
                            continue
                    else:
                        log(f"Skipping already emailed: {pdf}", log_box, log_file='gmail_log.txt')
                        continue
                log(f"Sending email for {pdf}... [To: {recipient}]", log_box, log_file='gmail_log.txt')
                try:
                    send_pdf_email(pdf, gmail_sender, gmail_pwd, recipient)
                    entry = {
                        'file_path': pdf,
                        'hospital_name': 'Gmail',
                        'upload_path': folder,
                        'upload_timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                    }
                    uploaded_list.append(entry)
                    new_uploads += 1
                    log(f"Email sent for {pdf}", log_box, log_file='gmail_log.txt')
                except Exception as ex:
                    log(f"Email failed for {pdf}: {ex}", log_box, log_file='gmail_log.txt')
                time.sleep(2)

            if new_uploads > 0:
                save_uploaded_files(uploaded_list)
                log(f"Saved {len(uploaded_list)} emailed files to disk.", log_box, log_file='gmail_log.txt')

        if once:
            log("Run-once mode completed.", log_box, log_file='gmail_log.txt')
            break
        
        cycle += 1

        log(f"Waiting {interval} seconds until next cycle...", log_box, log_file='gmail_log.txt')
        for _ in range(interval):
            if stop_event.is_set():
                log("Upload stopped by user.", log_box, log_file='gmail_log.txt')
                save_uploaded_files(uploaded_list)
                return
            time.sleep(1)

    save_uploaded_files(uploaded_list)


def browse_folder(entry):
    folder = filedialog.askdirectory()
    if folder:
        entry.delete(0, 'end')
        entry.insert(0, folder)


def browse_csv(entry):
    csv_path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
    if csv_path:
        entry.delete(0, 'end')
        entry.insert(0, csv_path)


def csv_gmail_worker(csv_file, folder, gmail_sender, gmail_pwd, log_box, stop_event):
    if not os.path.exists(csv_file):
        log(f"CSV file not found: {csv_file}", log_box, log_file='gmail_log.txt')
        return

    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        log(f"Failed to read CSV: {e}", log_box, log_file='gmail_log.txt')
        return

    email_regex = re.compile(r"^[_a-z0-9-]+(\.[a-z0-9-]+)*@[a-z0-9-]+(\.[a-z0-9-]+)*(\.[a-z]{2,4})$")
    
    log(f"Starting CSV processing for {len(lines)} lines...", log_box, log_file='gmail_log.txt')
    
    for i, line in enumerate(lines):
        if stop_event.is_set():
            log("CSV Upload stopped by user.", log_box, log_file='gmail_log.txt')
            return
            
        line_clean = line.strip('\n')
        values = line_clean.split(',')
        
        if len(values) >= 5:
            status = values[4].strip('"').strip()
            if status == "1":
                continue # Already sent
                
            email_address = values[3].strip()
            attachment_name = values[2].strip()
            file_path = os.path.join(folder, attachment_name)
            
            if os.path.isfile(file_path):
                if email_regex.match(email_address):
                    log(f"Sending CSV email to {email_address} with {attachment_name}...", log_box, log_file='gmail_log.txt')
                    try:
                        send_pdf_email(
                            file_path=file_path,
                            sender_email=gmail_sender,
                            sender_password=gmail_pwd,
                            recipient_email=email_address,
                            subject=values[1],
                            body=values[1]
                        )
                        lines[i] = line_clean + "1\n"
                        log(f"Success. Marked as '1'", log_box, log_file='gmail_log.txt')
                        
                        with open(csv_file, 'w', encoding='utf-8') as f:
                            f.writelines(lines)
                    except Exception as ex:
                        log(f"Email failed for {email_address}: {ex}", log_box, log_file='gmail_log.txt')
                        time.sleep(2)
                else:
                    log(f"Invalid email: {email_address}. Marking as '3'.", log_box, log_file='gmail_log.txt')
                    lines[i] = line_clean + "3\n"
                    with open(csv_file, 'w', encoding='utf-8') as f:
                        f.writelines(lines)
            else:
                if status == "":
                    log(f"File missing: {file_path}. Marking as '2'.", log_box, log_file='gmail_log.txt')
                    lines[i] = line_clean + "2\n"
                    with open(csv_file, 'w', encoding='utf-8') as f:
                        f.writelines(lines)
                
            time.sleep(1)

    # Summarize successes like VB code does on completion
    success_count = 0
    with open(csv_file, 'r', encoding='utf-8') as f:
        for final_line in f:
            v = final_line.strip('\n').split(',')
            if len(v) >= 5 and v[4].strip('"').strip() == "1":
                success_count += 1
                
    log(f"CSV processing completed. Total successfully sent: {success_count}", log_box, log_file='gmail_log.txt')


def whatsapp_worker(folder, secret, account, recipient, message, interval, once, log_box, stop_event):
    if interval <= 0:
        log("Interval must be a positive number.", log_box, log_file='whatsapp_log.txt')
        return

    uploaded_list = load_uploaded_files()
    log(f"Loaded {len(uploaded_list)} previously uploaded files for WhatsApp.", log_box, log_file='whatsapp_log.txt')
    
    cycle = 1
    while not stop_event.is_set():
        pdfs = find_pdfs(folder)
        if not pdfs:
            log(f"No PDF files found in folder {folder}.", log_box, log_file='whatsapp_log.txt')
        else:
            new_uploads = 0
            for pdf in pdfs:
                if stop_event.is_set():
                    log("WhatsApp Upload stopped by user.", log_box, log_file='whatsapp_log.txt')
                    save_uploaded_files(uploaded_list)
                    return
                # Check if file already uploaded
                already_uploaded = any(entry['file_path'] == pdf and entry.get('upload_type') == 'WhatsApp' for entry in uploaded_list)
                if already_uploaded:
                    if cycle >= 2:
                        response = messagebox.askyesno("File Already Uploaded", 
                            f"File already sent via WhatsApp:\n{pdf}\n\nDo you want to send it again?")
                        if not response:
                            log(f"Skipping already sent: {pdf}", log_box, log_file='whatsapp_log.txt')
                            continue
                    else:
                        log(f"Skipping already sent: {pdf}", log_box, log_file='whatsapp_log.txt')
                        continue

                log(f"Processing WhatsApp for {pdf}...", log_box, log_file='whatsapp_log.txt')
                try:
                    # 1. Extract patient name and mobile from PDF
                    log(f"Extracting patient data from {os.path.basename(pdf)}...", log_box, log_file='whatsapp_log.txt')
                    extractor = ReportExtractor()
                    patient_name = "Patient"
                    final_recipient = recipient # Fallback to GUI input

                    try:
                        data = extractor.extract(pdf)
                        if data.get('name'):
                            patient_name = data['name']
                            log(f"Extracted name: {patient_name}", log_box, log_file='whatsapp_log.txt')
                        
                        if data.get('mobile'):
                            # Basic cleaning: remove non-digits if needed, 
                            # but keeping simple for now as the extractor already cleans spaces.
                            extracted_mobile = data['mobile']
                            # Add country code if missing (assuming 91 for India as per previous requests)
                            if len(extracted_mobile) == 10:
                                extracted_mobile = "91" + extracted_mobile
                            
                            final_recipient = extracted_mobile
                            log(f"Extracted Receiver Number: {final_recipient}", log_box, log_file='whatsapp_log.txt')
                    except Exception as e:
                        log(f"Data extraction failed: {e}. Using GUI fallback.", log_box, log_file='whatsapp_log.txt')

                    # 2. Call Walocal API
                    log(f"Calling Walocal API to {final_recipient}...", log_box, log_file='whatsapp_log.txt')
                    response = send_whatsapp_api(
                        authkey=secret,
                        tempid=account,
                        mobile=final_recipient,
                        ph1=patient_name,
                        sender=WALOCAL_SENDER
                    )
                    
                    if response.status_code == 200:
                        entry = {
                            'file_path': pdf,
                            'hospital_name': 'WhatsApp',
                            'upload_type': 'WhatsApp',
                            'upload_path': folder,
                            'upload_timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                            'patient_name': patient_name,
                            'receiver_number': final_recipient
                        }
                        uploaded_list.append(entry)
                        new_uploads += 1
                        log(f"WhatsApp Success for {patient_name} ({final_recipient})", log_box, log_file='whatsapp_log.txt')
                    else:
                        log(f"WhatsApp Failed: {response.status_code} - {response.text}", log_box, log_file='whatsapp_log.txt')
                except Exception as ex:
                    log(f"WhatsApp failed for {pdf}: {ex}", log_box, log_file='whatsapp_log.txt')
                time.sleep(2)

            if new_uploads > 0:
                save_uploaded_files(uploaded_list)
                log(f"Saved {len(uploaded_list)} WhatsApp entries to disk.", log_box, log_file='whatsapp_log.txt')

        if once: break
        cycle += 1
        log(f"Waiting {interval} seconds until next cycle...", log_box, log_file='whatsapp_log.txt')
        for _ in range(interval):
            if stop_event.is_set(): break
            time.sleep(1)

    save_uploaded_files(uploaded_list)


def csv_whatsapp_worker(csv_file, folder, secret, account, log_box, stop_event):
    if not os.path.exists(csv_file):
        log(f"CSV file not found: {csv_file}", log_box, log_file='whatsapp_log.txt')
        return

    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        log(f"Failed to read CSV: {e}", log_box, log_file='whatsapp_log.txt')
        return

    log(f"Starting CSV WhatsApp processing for {len(lines)} lines...", log_box, log_file='whatsapp_log.txt')
    
    for i, line in enumerate(lines):
        if stop_event.is_set():
            log("WhatsApp CSV Batch stopped by user.", log_box, log_file='whatsapp_log.txt')
            return
            
        line_clean = line.strip('\n')
        values = line_clean.split(',')
        
        # Expected CSV format: ID, Message, Attachment_Name, Recipient_Phone, Status
        if len(values) >= 5:
            status = values[4].strip('"').strip()
            if status == "1": continue
                
            msg_text = values[1].strip()
            attachment_name = values[2].strip()
            phone_number = values[3].strip()
            log(f"Sending WhatsApp to {phone_number}...", log_box, log_file='whatsapp_log.txt')
            try:
                # Use msg_text as the ph1 (patient name) if possible, 
                # or just use a placeholder if the template expects it.
                response = send_whatsapp_api(
                    authkey=secret,
                    tempid=account,
                    mobile=phone_number,
                    ph1=msg_text if msg_text else "Patient",
                    sender=WALOCAL_SENDER
                )

                if response.status_code == 200:
                    lines[i] = line_clean + "1\n"
                    log(f"Success for {phone_number}", log_box, log_file='whatsapp_log.txt')
                    with open(csv_file, 'w', encoding='utf-8') as f:
                        f.writelines(lines)
                else:
                    log(f"Failed for {phone_number}: {response.text}", log_box, log_file='whatsapp_log.txt')
            except Exception as ex:
                log(f"WhatsApp failed: {ex}", log_box, log_file='whatsapp_log.txt')
            time.sleep(2)
            time.sleep(2)
def star_login(email, password):
    """Authenticate with Star Software Solution and return session + initial CSRF token."""
    session = requests.Session()
    # Laravel often expects these standard browser headers
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    
    login_page = "https://starsoftwaresolution.in/admin/login"
    try:
        r = session.get(login_page, timeout=15)
        csrf_token = re.search(r'name="_token" value="([^"]+)"', r.text)
        if not csrf_token:
            csrf_token = re.search(r'content="([^"]+)" name="csrf-token"', r.text)
        csrf_token = csrf_token.group(1) if csrf_token else ""
    except Exception as e:
        return None, f"Failed to reach login page: {e}"

    login_data = {
        '_token': csrf_token,
        'email': email,
        'password': password,
        'login_as': 'web',
        'remember': 'on'
    }
    
    post_headers = {
        'Origin': 'https://starsoftwaresolution.in',
        'Referer': login_page,
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    try:
        response = session.post("https://starsoftwaresolution.in/login", data=login_data, headers=post_headers, timeout=15)
    except Exception as e:
        return None, f"POST failed: {str(e)}"
    
    # 3. Check Success
    # Successful login redirects to /dashboard or similar
    if 'login' in response.url:
        # Check if there's an error message in the HTML
        if 'credentials' in response.text.lower():
            return None, "Invalid credentials according to website."
        return None, f"Login failed. Redirected back to: {response.url}"
        
    # 4. Success - Get the CSRF token for the dashboard/upload page
    # It might be in the meta tags of the current (redirected) page
    new_csrf_match = re.search(r'content="([^"]+)" name="csrf-token"', response.text)
    
    if new_csrf_match:
        return session, new_csrf_match.group(1)
    
    # If not in meta, try fetching upload page explicitly
    try:
        up_page = session.get('https://starsoftwaresolution.in/reports-upload', timeout=15)
        new_csrf_match = re.search(r'content="([^"]+)" name="csrf-token"', up_page.text)
        if new_csrf_match:
            return session, new_csrf_match.group(1)
    except: pass

    return session, csrf_token


def star_upload_pdf(session, file_path, csrf_token):
    """Upload a single PDF to Star Software Solution.
    
    The website uses a drag-drop zone with JavaScript AJAX:
    HTML input: <input type="file" id="dropZone" name="file" multiple />
    JS builds FormData, appends the file under field name 'pdf',
    then does $.ajax POST to /reports-upload with X-XSRF-TOKEN header.
    """
    url = "https://starsoftwaresolution.in/reports-upload"
    
    xsrf = urllib.parse.unquote(session.cookies.get('XSRF-TOKEN', ''))
    headers = {
        # Cookie-based CSRF: required for Laravel AJAX uploads
        'X-XSRF-TOKEN': xsrf,
        # Identifies this as an AJAX call (triggers JSON response instead of HTML)
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': 'https://starsoftwaresolution.in/reports-upload',
        'Origin': 'https://starsoftwaresolution.in',
    }
    
    with open(file_path, 'rb') as f:
        # The site's JavaScript sends the file under FormData key 'pdf'.
        files = {'pdf': (os.path.basename(file_path), f, 'application/pdf')}
        response = session.post(url, files=files, headers=headers, timeout=30)
        return response


def star_upload_csv(session, file_path, laboratory_id, csrf_token):
    """Upload/Import a CSV/Excel file to Star Software Solution.
    
    The website uses a regular HTML form POST (NOT AJAX):
    <form action="/reports/import" method="post" enctype="multipart/form-data">
        <input type="hidden" name="_token" value="...">   <- CSRF in form body
        <select name="laboratory_id">...</select>
        <input type="file" name="excel">                  <- file field name
    </form>
    
    Since this is a regular POST (not AJAX), we send both:
    - _token in form body (required for this route's CSRF check)
    - X-XSRF-TOKEN header (belt-and-braces)
    """
    url = "https://starsoftwaresolution.in/reports/import"
    
    xsrf = urllib.parse.unquote(session.cookies.get('XSRF-TOKEN', ''))
    headers = {
        'X-XSRF-TOKEN': xsrf,
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://starsoftwaresolution.in/reports-upload',
        'Origin': 'https://starsoftwaresolution.in',
    }
    # _token in form body is REQUIRED for this route (regular form CSRF)
    data = {
        '_token': csrf_token,
        'laboratory_id': laboratory_id,
    }
    ext = os.path.splitext(file_path)[1].lower()
    if ext in ('.xlsx', '.xls'):
        mime = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    else:
        mime = 'text/csv'
    with open(file_path, 'rb') as f:
        # Field name must be 'excel' to match the website's HTML:
        # <input class="form-control" type="file" name="excel" id="excel">
        files = {'excel': (os.path.basename(file_path), f, mime)}
        response = session.post(url, data=data, files=files, headers=headers, timeout=30)
        return response


HEALHUB_APP_URL = 'https://healhub-desk.vercel.app'
HEALHUB_SUPABASE_URL = 'https://helayhixzmtzssxjbrdg.supabase.co'
HEALHUB_SUPABASE_ANON_KEY = (
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
    'eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImhlbGF5aGl4em10enNzeGpicmRnIiwicm9sZSI6'
    'ImFub24iLCJpYXQiOjE3NjQ1ODA0NDAsImV4cCI6MjA4MDE1NjQ0MH0.'
    'BpuKg2uY2kNPnUENO8SaTaJwW9Un_SIPtsEtJjnefCw'
)
HEALHUB_STORAGE_BUCKET = 'lab-reports'
HEALHUB_MAX_PDF_SIZE = 2 * 1024 * 1024


def healhub_normalize_text(value):
    return str(value or '').replace('\u200b', '').replace('\u200c', '').replace('\u200d', '').replace('\ufeff', '').strip()


def healhub_normalize_key(value):
    return ' '.join(healhub_normalize_text(value).lower().replace('_', ' ').split())


def healhub_pdf_stem(value):
    text = healhub_normalize_text(value).lower()
    if text.endswith('.pdf'):
        text = text[:-4]
    return text


def healhub_parse_date_value(value):
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
        return datetime(value.year, value.month, value.day).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        base_date = datetime(1899, 12, 30)
        return (base_date + timedelta(days=float(value))).isoformat()

    text = healhub_normalize_text(value)
    if not text:
        return None

    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return None


def healhub_json_request(method, url, payload=None, headers=None, timeout=30):
    req_headers = {
        'apikey': HEALHUB_SUPABASE_ANON_KEY,
    }
    if headers:
        req_headers.update(headers)
    response = requests.request(method, url, json=payload, headers=req_headers, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f'HealHub request failed ({response.status_code}): {response.text[:300]}')
    if not response.text.strip():
        return None
    try:
        return response.json()
    except Exception:
        return response.text


def healhub_login(email, password):
    response = healhub_json_request(
        'POST',
        f'{HEALHUB_SUPABASE_URL}/auth/v1/token?grant_type=password',
        payload={'email': email.strip(), 'password': password},
        headers={'Content-Type': 'application/json'},
    )
    return {
        'access_token': response['access_token'],
        'user_id': response['user']['id'],
        'user': response['user'],
    }


def healhub_auth_headers(session_info, extra_headers=None):
    headers = {
        'Authorization': f"Bearer {session_info['access_token']}",
        'apikey': HEALHUB_SUPABASE_ANON_KEY,
    }
    if extra_headers:
        headers.update(extra_headers)
    return headers


def healhub_load_mapping_rows(mapping_file):
    ext = os.path.splitext(mapping_file)[1].lower()

    def normalize_row(row_dict):
        return {healhub_normalize_key(key): value for key, value in row_dict.items()}

    if ext == '.csv':
        with open(mapping_file, 'r', encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            return [normalize_row(row) for row in reader]

    if ext == '.xlsx':
        if openpyxl is None:
            raise RuntimeError('openpyxl is required for .xlsx mapping files.')
        workbook = openpyxl.load_workbook(mapping_file, data_only=True)
        sheet = workbook[workbook.sheetnames[0]]
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [healhub_normalize_text(value) for value in rows[0]]
        records = []
        for row in rows[1:]:
            row_dict = {}
            for index, header in enumerate(headers):
                row_dict[header] = row[index] if index < len(row) else ''
            records.append(normalize_row(row_dict))
        return records

    if ext == '.xls':
        raise RuntimeError('Please convert .xls mapping files to .xlsx or .csv before using HealHub bulk upload.')

    raise RuntimeError(f'Unsupported mapping file: {mapping_file}')


def healhub_collect_pdf_files(folder):
    pdf_files = []
    if folder and os.path.isdir(folder):
        for root_dir, _dirs, files in os.walk(folder):
            for file_name in sorted(files):
                if file_name.lower().endswith('.pdf'):
                    full_path = os.path.join(root_dir, file_name)
                    size = os.path.getsize(full_path)
                    if size > HEALHUB_MAX_PDF_SIZE:
                        raise RuntimeError(f'PDF exceeds 2MB limit: {file_name}')
                    pdf_files.append(full_path)
    return pdf_files


def healhub_find_lab_id(session_info, lab_value):
    raw_value = healhub_normalize_text(lab_value)
    if not raw_value:
        raise RuntimeError('HealHub laboratory is required.')

    response = requests.get(
        f'{HEALHUB_SUPABASE_URL}/rest/v1/laboratories?select=id,name,unique_id,status&limit=100',
        headers=healhub_auth_headers(session_info),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f'Could not load HealHub laboratories: {response.text[:300]}')

    try:
        labs = response.json()
    except Exception as exc:
        raise RuntimeError(f'Unexpected laboratory response: {exc}') from exc

    value_lower = raw_value.lower()
    for lab in labs:
        candidates = {
            str(lab.get('id') or '').lower(),
            healhub_normalize_text(lab.get('unique_id')).lower(),
            healhub_normalize_text(lab.get('name')).lower(),
        }
        if value_lower in candidates:
            return lab.get('id'), lab

    raise RuntimeError(f"HealHub laboratory '{raw_value}' was not found.")


def healhub_upload_pdf(session_info, file_path, lab_id, bulk_upload_id):
    object_path = f"{lab_id}/{bulk_upload_id}/{os.path.basename(file_path)}"
    encoded_path = '/'.join(urllib.parse.quote(part, safe='') for part in object_path.split('/'))
    url = f'{HEALHUB_SUPABASE_URL}/storage/v1/object/{HEALHUB_STORAGE_BUCKET}/{encoded_path}'
    with open(file_path, 'rb') as handle:
        response = requests.post(
            url,
            data=handle.read(),
            headers=healhub_auth_headers(
                session_info,
                {
                    'Content-Type': 'application/pdf',
                    'cache-control': '3600',
                    'x-upsert': 'false',
                },
            ),
            timeout=60,
        )
    if response.status_code >= 400:
        raise RuntimeError(f'PDF upload failed ({response.status_code}): {response.text[:300]}')
    public_path = '/'.join(urllib.parse.quote(part, safe='') for part in object_path.split('/'))
    return f'{HEALHUB_SUPABASE_URL}/storage/v1/object/public/{HEALHUB_STORAGE_BUCKET}/{public_path}'


def healhub_insert_bulk_upload(session_info, bulk_upload_id, lab_id, total_files):
    requests.post(
        f'{HEALHUB_SUPABASE_URL}/rest/v1/bulk_uploads',
        json={
            'id': bulk_upload_id,
            'lab_id': lab_id,
            'status': 'processing',
            'total_files': total_files,
            'uploaded_by': session_info['user_id'],
        },
        headers=healhub_auth_headers(session_info, {'Content-Type': 'application/json', 'Prefer': 'return=minimal'}),
        timeout=30,
    ).raise_for_status()


def healhub_update_bulk_upload(session_info, bulk_upload_id, processed_files):
    requests.patch(
        f"{HEALHUB_SUPABASE_URL}/rest/v1/bulk_uploads?id=eq.{urllib.parse.quote(bulk_upload_id, safe='')}",
        json={
            'status': 'completed',
            'processed_files': processed_files,
            'updated_at': datetime.utcnow().isoformat(),
        },
        headers=healhub_auth_headers(session_info, {'Content-Type': 'application/json', 'Prefer': 'return=minimal'}),
        timeout=30,
    ).raise_for_status()


def healhub_insert_lab_report(session_info, payload):
    response = requests.post(
        f'{HEALHUB_SUPABASE_URL}/rest/v1/lab_reports',
        json=payload,
        headers=healhub_auth_headers(session_info, {'Content-Type': 'application/json', 'Prefer': 'return=minimal'}),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f'Lab report insert failed ({response.status_code}): {response.text[:300]}')


def healhub_bulk_upload_worker(pdf_folder, mapping_file, email, password, lab_value, log_box, stop_event):
    if not pdf_folder or not os.path.isdir(pdf_folder):
        log('❌ HealHub bulk upload requires a valid PDF folder.', log_box, log_file='healhub_bulk_log.txt')
        return
    if not mapping_file or not os.path.isfile(mapping_file):
        log('❌ HealHub bulk upload requires a valid mapping file (.csv or .xlsx).', log_box, log_file='healhub_bulk_log.txt')
        return

    try:
        pdf_files = healhub_collect_pdf_files(pdf_folder)
        if not pdf_files:
            log('❌ No PDF files found for HealHub bulk upload.', log_box, log_file='healhub_bulk_log.txt')
            return

        log(f'Connecting to HealHub ({HEALHUB_APP_URL})...', log_box, log_file='healhub_bulk_log.txt')
        session_info = healhub_login(email, password)
        lab_id, lab_info = healhub_find_lab_id(session_info, lab_value)
        log(
            f"✓ HealHub login successful. Using lab: {lab_info.get('name', 'Unknown')} ({lab_info.get('unique_id', '-')})",
            log_box,
            log_file='healhub_bulk_log.txt',
        )

        mapping_rows = healhub_load_mapping_rows(mapping_file)
        bulk_upload_id = str(uuid.uuid4())
        healhub_insert_bulk_upload(session_info, bulk_upload_id, lab_id, len(pdf_files))
        log(
            f'Created HealHub bulk upload session {bulk_upload_id} for {len(pdf_files)} PDF file(s).',
            log_box,
            log_file='healhub_bulk_log.txt',
        )

        processed = 0
        failed = 0
        for index, file_path in enumerate(pdf_files, start=1):
            if stop_event.is_set():
                log('HealHub bulk upload stopped by user.', log_box, log_file='healhub_bulk_log.txt')
                break

            file_name = os.path.basename(file_path)
            try:
                public_url = healhub_upload_pdf(session_info, file_path, lab_id, bulk_upload_id)
                mapping_row = None
                pdf_key = healhub_pdf_stem(file_name)
                for row in mapping_rows:
                    if healhub_pdf_stem(row.get('report pdi')) == pdf_key:
                        mapping_row = row
                        break

                upload_date = healhub_parse_date_value((mapping_row or {}).get('date'))
                unique_id = healhub_normalize_text((mapping_row or {}).get('unique id')) or pdf_key
                patient_name = (
                    healhub_normalize_text((mapping_row or {}).get('name'))
                    or healhub_normalize_text((mapping_row or {}).get('patient name'))
                    or 'Unknown'
                )
                mobile = healhub_normalize_text((mapping_row or {}).get('mobile') or (mapping_row or {}).get('phone')) or None

                payload = {
                    'unique_id': unique_id,
                    'patient_name': patient_name,
                    'mobile': mobile,
                    'pdf_url': public_url,
                    'lab_id': lab_id,
                    'bulk_upload_id': bulk_upload_id,
                    'created_by': session_info['user_id'],
                }
                if upload_date:
                    payload['upload_date'] = upload_date

                healhub_insert_lab_report(session_info, payload)
                processed += 1
                log(f'[{index}/{len(pdf_files)}] ✓ HealHub uploaded {file_name}', log_box, log_file='healhub_bulk_log.txt')
            except Exception as exc:
                failed += 1
                log(f'[{index}/{len(pdf_files)}] ❌ HealHub failed {file_name}: {exc}', log_box, log_file='healhub_bulk_log.txt')

        healhub_update_bulk_upload(session_info, bulk_upload_id, processed)
        log(
            f'HealHub bulk upload finished. Processed: {processed}, Failed: {failed}, Session: {bulk_upload_id}',
            log_box,
            log_file='healhub_bulk_log.txt',
        )
    except Exception as exc:
        log(f'❌ HealHub bulk upload error: {exc}', log_box, log_file='healhub_bulk_log.txt')


def star_worker(folder, csv_file, email, password, lab_id, interval, once, log_box, stop_event):
    """Worker thread for Star Software Solution automation."""
    if interval <= 0:
        interval = 5

    log(f"Logging in to Star Software Solution as {email}...", log_box, log_file='star_log.txt')
    session, csrf_token = star_login(email, password)
    if not session:
        log(f"❌ Login failed: {csrf_token}", log_box, log_file='star_log.txt')
        return
    log("✓ Login successful.", log_box, log_file='star_log.txt')

    uploaded_list = load_uploaded_files()
    log(f"Loaded {len(uploaded_list)} previously uploaded files.", log_box, log_file='star_log.txt')
    
    cycle = 1
    while not stop_event.is_set():
        # Find PDFs and CSV/Excel files
        all_files = []
        if folder and os.path.isdir(folder):
            for root, dirs, files in os.walk(folder):
                for file in files:
                    if file.lower().endswith(('.pdf', '.csv', '.xlsx', '.xls')):
                        all_files.append(os.path.join(root, file))
        
        if csv_file:
            if os.path.isdir(csv_file):
                for root, dirs, files in os.walk(csv_file):
                    for file in files:
                        if file.lower().endswith(('.csv', '.xlsx', '.xls')):
                            full_path = os.path.join(root, file)
                            if full_path not in all_files:
                                all_files.append(full_path)
            elif os.path.isfile(csv_file):
                if csv_file not in all_files:
                    all_files.append(csv_file)

        if not all_files:
            log("No PDF or CSV/Excel files found. Please check your folder or CSV selection.", log_box, log_file='star_log.txt')
        else:
            # Import works reliably when PDFs are uploaded before CSV/Excel rows that reference them.
            all_files = sorted(all_files, key=lambda p: (0 if p.lower().endswith('.pdf') else 1, os.path.basename(p).lower()))
            new_uploads = 0
            for file_path in all_files:
                if stop_event.is_set(): break
                
                # Check duplication
                already_uploaded = any(entry['file_path'] == file_path and entry.get('upload_type') == 'Star' for entry in uploaded_list)
                if already_uploaded:
                    continue

                # REFRESH TOKEN IMMEDIATELY BEFORE EACH UPLOAD
                # Fetches a fresh CSRF token AND updates the XSRF-TOKEN cookie
                try:
                    up_page = session.get('https://starsoftwaresolution.in/reports-upload', timeout=15)
                    if up_page.status_code == 200:
                        for pat in [
                            r'<meta name="csrf-token" content="([^"]+)"',
                            r'content="([^"]+)" name="csrf-token"',
                            r'name="_token" value="([^"]+)"',
                        ]:
                            fresh_token_match = re.search(pat, up_page.text)
                            if fresh_token_match:
                                csrf_token = fresh_token_match.group(1)
                                break
                    else:
                        log(f"⚠️  Token refresh returned {up_page.status_code} - session may have expired.",
                            log_box, log_file='star_log.txt')
                except Exception as te:
                    log(f"⚠️  Token refresh error: {te}", log_box, log_file='star_log.txt')

                log(f"Uploading {os.path.basename(file_path)} to Star Software...", log_box, log_file='star_log.txt')
                try:
                    if file_path.lower().endswith('.pdf'):
                        response = star_upload_pdf(session, file_path, csrf_token)
                    else:
                        response = star_upload_csv(session, file_path, lab_id, csrf_token)

                    # Parse JSON response if possible
                    resp_json = None
                    try:
                        resp_json = response.json()
                    except Exception:
                        pass

                    success = False
                    if response.status_code in (200, 201):
                        if resp_json is not None:
                            if resp_json.get('status') is True:
                                success = True
                                log(f"✓ Success: {os.path.basename(file_path)} — {resp_json.get('message', 'OK')}",
                                    log_box, log_file='star_log.txt')
                            else:
                                # Server returned 200 but with status:false (validation error)
                                err_msg = resp_json.get('message', str(resp_json))
                                log(f"❌ Server rejected: {os.path.basename(file_path)} — {err_msg}",
                                    log_box, log_file='star_log.txt')
                        else:
                            content_type = response.headers.get('Content-Type', '')
                            content_disposition = response.headers.get('Content-Disposition', '')
                            # Got HTML back instead of JSON (likely CSRF failure / redirect)
                            if 'attachment' in content_disposition.lower() and 'sheet' in content_type.lower():
                                dump_name = 'star_upload_error_report.xlsx'
                                with open(dump_name, 'wb') as df:
                                    df.write(response.content)
                                log(f"❌ Import rejected for {os.path.basename(file_path)} — Star returned {dump_name}.",
                                    log_box, log_file='star_log.txt')
                            elif 'login' in response.url.lower():
                                log(f"❌ Session expired during upload — please restart Star upload.",
                                    log_box, log_file='star_log.txt')
                            elif response.url.rstrip('/').endswith('reports-upload'):
                                log(f"❌ Redirected back to upload page — CSRF token mismatch or route issue.",
                                    log_box, log_file='star_log.txt')
                            else:
                                log(f"❌ Unexpected HTML response for {os.path.basename(file_path)} — see star_upload_fail_dump.html",
                                    log_box, log_file='star_log.txt')
                            with open('star_upload_fail_dump.html', 'w', encoding='utf-8') as df:
                                df.write(f"URL: {response.url}\nStatus: {response.status_code}\n\n")
                                df.write(response.text)
                    elif response.status_code == 401:
                        log(f"❌ 401 Unauthenticated — session expired. Please restart Star upload.",
                            log_box, log_file='star_log.txt')
                    elif response.status_code == 419:
                        log(f"❌ 419 CSRF Token Mismatch for {os.path.basename(file_path)} — token expired.",
                            log_box, log_file='star_log.txt')
                    elif response.status_code == 422:
                        err_msg = resp_json.get('message', response.text[:200]) if resp_json else response.text[:200]
                        log(f"❌ 422 Validation Error: {err_msg}", log_box, log_file='star_log.txt')
                    else:
                        log(f"❌ Failed: HTTP {response.status_code} for {os.path.basename(file_path)}",
                            log_box, log_file='star_log.txt')
                        with open('star_upload_fail_dump.html', 'w', encoding='utf-8') as df:
                            df.write(f"URL: {response.url}\nStatus: {response.status_code}\n\n")
                            df.write(response.text)

                    if success:
                        entry = {
                            'file_path': file_path,
                            'hospital_name': 'Star Software',
                            'upload_type': 'Star',
                            'upload_path': folder or (os.path.dirname(csv_file) if csv_file else ''),
                            'upload_timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                        }
                        uploaded_list.append(entry)
                        new_uploads += 1

                except Exception as ex:
                    log(f"❌ Upload error for {os.path.basename(file_path)}: {ex}", log_box, log_file='star_log.txt')
                time.sleep(1)

            if new_uploads > 0:
                save_uploaded_files(uploaded_list)

        if once: break
        cycle += 1
        log(f"Waiting {interval} seconds until next cycle...", log_box, log_file='star_log.txt')
        for _ in range(interval):
            if stop_event.is_set(): break
            time.sleep(1)

    log("Star Software automation stopped.", log_box, log_file='star_log.txt')



def restore_button_states(stop_btn, *buttons):
    for button in buttons:
        button.configure(state='normal')
    stop_btn.configure(state='disabled')


def start_app(folder_entry, hospital_entry, password_entry, kvp_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, stop_btn, stop_event):
    log("Checking API Upload settings...", log_box, log_file='api_log.txt')
    folder = folder_entry.get().strip()
    hosp = hospital_entry.get().strip()
    pwd = password_entry.get().strip()
    kvp_text = kvp_entry.get().strip() or '{}'
    interval_value = interval_entry.get().strip() or '2'

    if not folder or not os.path.isdir(folder):
        log("❌ Error: Invalid or empty PDF Folder path.", log_box, log_file='api_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path.')
        return
    if not hosp or not pwd:
        log("❌ Error: Hospital name and password are required.", log_box, log_file='api_log.txt')
        messagebox.showerror('Error', 'Hospital name and password are required.')
        return

    try:
        kvp = json.loads(kvp_text)
    except json.JSONDecodeError:
        log("❌ Error: Invalid JSON for API key-value pairs.", log_box, log_file='api_log.txt')
        messagebox.showerror('Error', 'Invalid JSON for key_value_pairs.')
        return

    try:
        interval = int(interval_value)
        if interval <= 0:
            raise ValueError
    except ValueError:
        log("❌ Error: Interval must be a positive integer.", log_box, log_file='api_log.txt')
        messagebox.showerror('Error', 'Interval must be a positive integer number of seconds.')
        return

    log(f"🚀 Starting API Upload (Interval: {interval}s)...", log_box, log_file='api_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def api_upload_thread():
        try:
            worker(folder, hosp, pwd, kvp, interval, once_var.get(), log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn)

    threading.Thread(target=api_upload_thread, daemon=True).start()


def stop_app(start_btn, gmail_start_btn, stop_btn, stop_event):
    stop_event.set()
    start_btn.configure(state='normal')
    gmail_start_btn.configure(state='normal')
    stop_btn.configure(state='disabled')


def get_microphone_names():
    if sr is None:
        return []
    try:
        return sr.Microphone.list_microphone_names()
    except Exception:
        return []


def voice_fill_field(target_entry, log_box, mic_index=None, use_online=True, is_password=False):
    if sd is None or np is None:
        messagebox.showerror('Voice feature unavailable', 'Voice input requires sounddevice and numpy.')
        return

    # Mic selection log
    if mic_index is None:
        log('Using default system microphone', log_box)
    else:
        log(f'Using microphone index {mic_index}', log_box)

    popup = tk.Toplevel()
    popup.title('Voice Recording')
    popup.geometry('300x100')
    popup.transient(target_entry.winfo_toplevel())
    popup.grab_set()

    status_lbl = tk.Label(popup, text='Recording... (5 seconds)', fg='red', pady=10, font=('Arial', 12, 'bold'))
    status_lbl.pack()

    # Audio recording state
    audio_data = []
    stop_recording = threading.Event()
    
    def callback(indata, frames, time, status):
        if status:
            log(f"Audio input status: {status}", log_box)
        audio_data.append(indata.copy())

    def record_thread():
        try:
            with sd.InputStream(samplerate=16000, channels=1, callback=callback, device=mic_index):
                while not stop_recording.is_set():
                    time.sleep(0.1)
        except Exception as e:
            log(f"Recording error: {e}", log_box)
            stop_recording.set()

    t = threading.Thread(target=record_thread, daemon=True)
    t.start()

    # Auto-stop after 5 seconds
    def auto_stop():
        time.sleep(5)
        stop_recording.set()
        status_lbl.config(text="Processing audio...", fg="blue")
        popup.update()
        
        # Combine chunks
        if not audio_data:
            log("No audio recorded.", log_box)
            popup.destroy()
            return

        full_audio = np.concatenate(audio_data, axis=0)
        
        # Save to temp WAV file
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_file:
            tmp_filename = tmp_file.name
        
        try:
            with wave.open(tmp_filename, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2) # 16-bit
                wf.setframerate(16000)
                wf.writeframes((full_audio * 32767).astype(np.int16).tobytes())

            # Recognize
            recognize_audio_file(tmp_filename, target_entry, log_box, use_online, is_password)
        finally:
            if os.path.exists(tmp_filename):
                try: os.remove(tmp_filename)
                except: pass
            popup.destroy()

    stop_thread = threading.Thread(target=auto_stop, daemon=True)
    stop_thread.start()

def convert_word_numbers_to_digits(text):
    """Convert spoken numbers like 'password one two three' to 'password123'.
    Also handles mixed input like 'password$one' -> 'password$1'"""
    word_to_num = {
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
        'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9'
    }
    
    result = text.lower()
    
    # Replace each number word with its digit (using word boundaries)
    for word, digit in word_to_num.items():
        result = re.sub(r'\b' + word + r'\b', digit, result)
    
    # Remove all spaces to create continuous password
    result = result.replace(' ', '')
    
    return result


def voice_command_listener(root, log_box, stop_event, voice_active_event, start_btn, start_gmail_btn, start_csv_btn, whatsapp_start_btn, whatsapp_csv_btn, star_start_btn, stop_btn,
                        folder_entry, hospital_entry, password_entry, kvp_entry, interval_entry, once_var,
                        gmail_sender_entry, gmail_password_entry, recipient_entry, csv_entry, 
                        whatsapp_secret_entry, whatsapp_account_entry, whatsapp_recipient_entry, whatsapp_message_entry, 
                        star_email_entry, star_password_entry, star_lab_id_entry, global_stop_event):
    """Continuously listen for voice commands and execute them (when voice_active_event is set)."""
    if sr is None:
        log("Speech recognition not available for voice commands.", log_box)
        return
    
    r = sr.Recognizer()
    
    while not stop_event.is_set():
        # Only listen if voice commands are enabled
        if not voice_active_event.is_set():
            time.sleep(0.5)
            continue
        
        try:
            with sr.Microphone() as source:
                # Set a short timeout to check stop_event frequently
                r.adjust_for_ambient_noise(source, duration=0.5)
                audio = r.listen(source, timeout=2, phrase_time_limit=6)
            
            try:
                command = r.recognize_google(audio).lower()
                log(f"🎤 Command heard: {command}", log_box)
                
                # Parse and execute commands
                if 'start' in command and 'upload' in command and 'gmail' not in command and 'csv' not in command:
                    log("→ Executing: Start API Upload", log_box)
                    start_app(folder_entry, hospital_entry, password_entry, kvp_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, stop_btn, global_stop_event)
                
                elif 'start' in command and 'gmail' in command:
                    log("→ Executing: Start Gmail Upload", log_box)
                    gmail_start_app(folder_entry, gmail_sender_entry, gmail_password_entry, recipient_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, stop_btn, global_stop_event)
                
                elif 'start' in command and 'csv' in command and 'whatsapp' not in command:
                    log("→ Executing: Start CSV Batch", log_box)
                    csv_start_app(folder_entry, csv_entry, gmail_sender_entry, gmail_password_entry, log_box, start_btn, start_gmail_btn, start_csv_btn, stop_btn, global_stop_event)
                
                elif 'start' in command and 'whatsapp' in command and 'csv' not in command:
                    log("→ Executing: Start WhatsApp Upload", log_box)
                    whatsapp_start_app(folder_entry, whatsapp_secret_entry, whatsapp_account_entry, whatsapp_recipient_entry, whatsapp_message_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, whatsapp_start_btn, stop_btn, global_stop_event)

                elif 'start' in command and 'whatsapp' in command and 'csv' in command:
                    log("→ Executing: Start WhatsApp CSV Batch", log_box)
                    whatsapp_csv_start_app(folder_entry, csv_entry, whatsapp_secret_entry, whatsapp_account_entry, log_box, start_btn, start_gmail_btn, whatsapp_start_btn, whatsapp_csv_btn, stop_btn, global_stop_event)
                
                elif 'start' in command and 'star' in command:
                    log("→ Executing: Start Star Upload", log_box)
                    # Correctly passing csv_entry to star_start_app
                    star_start_app(folder_entry, csv_entry, star_email_entry, star_password_entry, star_lab_id_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, star_start_btn, whatsapp_csv_btn, stop_btn, global_stop_event)
                
                elif 'stop' in command:
                    log("→ Executing: Stop Upload", log_box)
                    global_stop_event.set()
                    start_btn.configure(state='normal')
                    start_gmail_btn.configure(state='normal')
                    start_csv_btn.configure(state='normal')
                    whatsapp_start_btn.configure(state='normal')
                    whatsapp_csv_btn.configure(state='normal')
                    star_start_btn.configure(state='normal')
                    star_csv_btn.configure(state='normal')
                    stop_btn.configure(state='disabled')
                
                elif 'clear' in command:
                    log("→ Executing: Clear Uploaded List", log_box)
                    clear_uploaded_list(log_box)
                
                elif 'hospital' in command:
                    # Extract hospital name after "hospital"
                    parts = command.split('hospital')
                    if len(parts) > 1:
                        hosp_name = parts[1].strip()
                        hospital_entry.delete(0, 'end')
                        hospital_entry.insert(0, hosp_name)
                        log(f"→ Hospital set to: {hosp_name}", log_box)
                
                elif 'password' in command:
                    # Extract and convert password
                    parts = command.split('password')
                    if len(parts) > 1:
                        pwd_text = parts[1].strip()
                        pwd_converted = convert_word_numbers_to_digits(pwd_text)
                        password_entry.delete(0, 'end')
                        password_entry.insert(0, pwd_converted)
                        log(f"→ Password set (heard: '{pwd_text}' → '{pwd_converted}')", log_box)
                
                elif 'interval' in command or 'seconds' in command:
                    # Try to extract a number
                    import re as regex_module
                    nums = regex_module.findall(r'\d+', command)
                    if nums:
                        interval_entry.delete(0, 'end')
                        interval_entry.insert(0, nums[0])
                        log(f"→ Interval set to: {nums[0]} seconds", log_box)
                
                elif 'recipient' in command or 'email' in command:
                    # Extract email
                    parts = command.split('recipient') if 'recipient' in command else command.split('email')
                    if len(parts) > 1:
                        email = parts[1].strip()
                        recipient_entry.delete(0, 'end')
                        recipient_entry.insert(0, email)
                        log(f"→ Recipient email set to: {email}", log_box)

                elif 'whatsapp secret' in command:
                    parts = command.split('whatsapp secret')
                    if len(parts) > 1:
                        secret = parts[1].strip()
                        whatsapp_secret_entry.delete(0, 'end')
                        whatsapp_secret_entry.insert(0, secret)
                        log(f"→ WhatsApp secret set", log_box)

                elif 'whatsapp account' in command:
                    parts = command.split('whatsapp account')
                    if len(parts) > 1:
                        acc = parts[1].strip()
                        whatsapp_account_entry.delete(0, 'end')
                        whatsapp_account_entry.insert(0, acc)
                        log(f"→ WhatsApp account set to: {acc}", log_box)

                elif 'whatsapp recipient' in command:
                    parts = command.split('whatsapp recipient')
                    if len(parts) > 1:
                        num = parts[1].strip().replace(' ', '')
                        whatsapp_recipient_entry.delete(0, 'end')
                        whatsapp_recipient_entry.insert(0, num)
                        log(f"→ WhatsApp recipient set to: {num}", log_box)
                
                else:
                    log(f"⚠️  Command not recognized: '{command}'", log_box)
            
            except sr.UnknownValueError:
                pass  # Silently ignore if not understood
            except sr.RequestError as e:
                log(f"Voice command error: {e}", log_box)
        
        except sr.WaitTimeoutError:
            pass  # Timeout, continue listening
        except Exception as e:
            log(f"Voice listener error: {e}", log_box)
        
        # Small delay to avoid CPU overload
        time.sleep(0.1)




def recognize_audio_file(wav_path, target_entry, log_box, use_online, is_password=False):
    if sr is None: return
    
    r = sr.Recognizer()
    with sr.AudioFile(wav_path) as source:
        audio = r.record(source)

    text = None
    try:
        if use_online:
            log('Recognizing online (Google)...', log_box)
            text = r.recognize_google(audio)
        else:
            if whisper is not None:
                log('Recognizing offline (Whisper)...', log_box)
                # Using sr.recognize_whisper which uses the whisper library correctly
                text = r.recognize_whisper(audio)
            else:
                log('Offline recognizer not ready, falling back to Google...', log_box)
                text = r.recognize_google(audio)
    except Exception as e:
        log(f"Recognition error: {e}", log_box)

    text = (text or "").strip()
    if text:
        # Convert word numbers to digits for password fields
        if is_password:
            converted = convert_word_numbers_to_digits(text)
            if converted:
                log(f'Heard: {text} → Converting to: {converted}', log_box)
                text = converted
        
        target_entry.delete(0, 'end')
        target_entry.insert(0, text)
        log(f'Success! Entered: {text}', log_box)
    else:
        log("Could not understand audio.", log_box)



def voice_fill_entry(target_entry, log_box, mic_index=None, use_online=True):
    voice_fill_field(target_entry, log_box, mic_index, use_online)


def voice_fill_password(password_entry, log_box, mic_index=None, use_online=True):
    voice_fill_field(password_entry, log_box, mic_index, use_online, is_password=True)


def voice_fill_hospital(hospital_entry, log_box, mic_index=None, use_online=True):
    voice_fill_field(hospital_entry, log_box, mic_index, use_online)


def clear_uploaded_list(log_box):
    uploaded_file = 'uploaded_files.txt'
    if os.path.exists(uploaded_file):
        os.remove(uploaded_file)
        log("Cleared uploaded files list.", log_box)
    else:
        log("No uploaded files list to clear.", log_box)


def gmail_start_app(folder_entry, gmail_sender_entry, gmail_password_entry, recipient_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, stop_btn, stop_event):
    log("Checking Gmail Upload settings...", log_box, log_file='gmail_log.txt')
    folder = folder_entry.get().strip()
    gmail_sender = gmail_sender_entry.get().strip()
    gmail_pwd = gmail_password_entry.get().strip()
    recipient = recipient_entry.get().strip()
    interval_value = interval_entry.get().strip() or '2'

    if not folder or not os.path.isdir(folder):
        log("❌ Error: Invalid or empty PDF Folder path.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path.')
        return
    if not gmail_sender or not gmail_pwd or not recipient:
        log("❌ Error: Gmail sender, password, and recipient are required.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Gmail sender, app password, and recipient email are required.')
        return

    try:
        interval = int(interval_value)
        if interval <= 0:
            raise ValueError
    except ValueError:
        log("❌ Error: Interval must be a positive integer.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Interval must be a positive integer number of seconds.')
        return

    log(f"🚀 Starting Gmail Upload (Interval: {interval}s)...", log_box, log_file='gmail_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    if 'csv_btn' in locals() or 'csv_start_btn' in globals():
        pass # Will disable all global buttons via another mechanism or simply not worry about it, wait, the stop_app logic handles it
    stop_btn.configure(state='normal')
    try:
        start_backend_tracked_worker(
            channel='gmail',
            channel_label='Gmail Upload',
            log_box=log_box,
            stop_event=stop_event,
            worker_func=gmail_worker,
            worker_args=(folder, gmail_sender, gmail_pwd, recipient, interval, once_var.get(), log_box, stop_event),
            source_path=folder,
            source_file_name=os.path.basename(folder.rstrip('\\/')) or 'gmail-upload-session',
            file_paths=find_pdfs(folder),
            notes=f'Gmail upload session to recipient {recipient}.',
        )
    except Exception as exc:
        start_btn.configure(state='normal')
        gmail_start_btn.configure(state='normal')
        stop_btn.configure(state='disabled')
        log(f"❌ Error: Could not create backend job for Gmail Upload: {exc}", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Secure Server Error', f'Could not create backend job:\n{exc}')
        return


def csv_start_app(folder_entry, csv_entry, gmail_sender_entry, gmail_password_entry, log_box, start_btn, gmail_start_btn, csv_start_btn, stop_btn, stop_event):
    log("Checking CSV Batch settings...", log_box, log_file='gmail_log.txt')
    folder = folder_entry.get().strip()
    csv_file = csv_entry.get().strip()
    gmail_sender = gmail_sender_entry.get().strip()
    gmail_pwd = gmail_password_entry.get().strip()

    if not folder or not os.path.isdir(folder):
        log("❌ Error: Invalid or empty PDF Folder path.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path for attachments.')
        return
    if not csv_file or not os.path.isfile(csv_file):
        log("❌ Error: Valid CSV file is required.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Please provide a valid CSV file path.')
        return
    if not gmail_sender or not gmail_pwd:
        log("❌ Error: Gmail sender and password are required.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Gmail sender and app password are required for CSV sending.')
        return

    log(f"🚀 Starting Gmail CSV Batch...", log_box, log_file='gmail_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    csv_start_btn.configure(state='disabled')
    stop_btn.configure(state='normal')
    try:
        start_backend_tracked_worker(
            channel='gmail',
            channel_label='Gmail CSV Batch',
            log_box=log_box,
            stop_event=stop_event,
            worker_func=csv_gmail_worker,
            worker_args=(csv_file, folder, gmail_sender, gmail_pwd, log_box, stop_event),
            source_path=csv_file,
            source_file_name=os.path.basename(csv_file),
            file_paths=collect_csv_attachment_paths(csv_file, folder),
            notes='CSV-driven Gmail batch session.',
        )
    except Exception as exc:
        start_btn.configure(state='normal')
        gmail_start_btn.configure(state='normal')
        csv_start_btn.configure(state='normal')
        stop_btn.configure(state='disabled')
        log(f"❌ Error: Could not create backend job for Gmail CSV Batch: {exc}", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Secure Server Error', f'Could not create backend job:\n{exc}')
        return


def whatsapp_start_app(folder_entry, whatsapp_secret_entry, whatsapp_account_entry, whatsapp_recipient_entry, whatsapp_message_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, whatsapp_start_btn, stop_btn, stop_event):
    log("Checking WhatsApp settings...", log_box, log_file='whatsapp_log.txt')
    folder = folder_entry.get().strip()
    secret = whatsapp_secret_entry.get().strip()
    account = whatsapp_account_entry.get().strip()
    recipient = whatsapp_recipient_entry.get().strip()
    message = whatsapp_message_entry.get().strip()
    interval_value = interval_entry.get().strip() or '2'

    if not folder or not os.path.isdir(folder):
        log("❌ Error: Invalid or empty PDF Folder path.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path.')
        return
    if not secret or not account or not recipient:
        log("❌ Error: WhatsApp Secret, Account, and Recipient are required.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'WhatsApp Secret, Account ID, and Recipient are required.')
        return

    try:
        interval = int(interval_value)
    except ValueError:
        interval = 2

    log(f"🚀 Starting WhatsApp Upload (Interval: {interval}s)...", log_box, log_file='whatsapp_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    whatsapp_start_btn.configure(state='disabled')
    stop_btn.configure(state='normal')
    try:
        start_backend_tracked_worker(
            channel='whatsapp',
            channel_label='WhatsApp Upload',
            log_box=log_box,
            stop_event=stop_event,
            worker_func=whatsapp_worker,
            worker_args=(folder, secret, account, recipient, message, interval, once_var.get(), log_box, stop_event),
            source_path=folder,
            source_file_name=os.path.basename(folder.rstrip('\\/')) or 'whatsapp-upload-session',
            file_paths=find_pdfs(folder),
            notes=f'WhatsApp upload session to recipient {recipient}.',
        )
    except Exception as exc:
        start_btn.configure(state='normal')
        gmail_start_btn.configure(state='normal')
        whatsapp_start_btn.configure(state='normal')
        stop_btn.configure(state='disabled')
        log(f"❌ Error: Could not create backend job for WhatsApp Upload: {exc}", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Secure Server Error', f'Could not create backend job:\n{exc}')
        return


def star_start_app(folder_entry, csv_entry, star_email_entry, star_password_entry, lab_id_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, star_start_btn, star_csv_btn, stop_btn, stop_event):
    log("Checking Star Software settings...", log_box, log_file='star_log.txt')
    folder = folder_entry.get().strip()
    csv_file = csv_entry.get().strip()
    email = star_email_entry.get().strip()
    pwd = star_password_entry.get().strip()
    lab_id = lab_id_entry.get().strip() or '1'
    interval_value = interval_entry.get().strip() or '5'

    # Check if at least one source is valid
    folder_valid = folder and os.path.isdir(folder)
    csv_valid = csv_file and (os.path.isfile(csv_file) or os.path.isdir(csv_file))
    
    if not folder_valid and not csv_valid:
        log("❌ Error: Please provide a valid PDF Folder OR a CSV Directory/File.", log_box, log_file='star_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path or a specific CSV directory/file.')
        return
        
    if not email or not pwd:
        log("❌ Error: Star Email and Password are required.", log_box, log_file='star_log.txt')
        messagebox.showerror('Error', 'Star Email and Password are required.')
        return

    try:
        interval = int(interval_value)
    except ValueError:
        interval = 5

    log(f"🚀 Starting Star Software Upload (Interval: {interval}s, Lab ID: {lab_id})...", log_box, log_file='star_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    star_start_btn.configure(state='disabled')
    star_csv_btn.configure(state='disabled')
    stop_btn.configure(state='normal')
    try:
        lab_id_value = int(lab_id) if str(lab_id).isdigit() else None
        start_backend_tracked_worker(
            channel='star',
            channel_label='Star Upload',
            log_box=log_box,
            stop_event=stop_event,
            worker_func=star_worker,
            worker_args=(folder, csv_file, email, pwd, lab_id, interval, once_var.get(), log_box, stop_event),
            source_path=folder or csv_file,
            source_file_name=os.path.basename((csv_file or folder).rstrip('\\/')) if (csv_file or folder) else 'star-upload-session',
            file_paths=collect_star_job_files(folder, csv_file),
            notes=f'Star upload session for lab {lab_id}.',
            lab_id=lab_id_value,
        )
    except Exception as exc:
        start_btn.configure(state='normal')
        gmail_start_btn.configure(state='normal')
        star_start_btn.configure(state='normal')
        star_csv_btn.configure(state='normal')
        stop_btn.configure(state='disabled')
        log(f"❌ Error: Could not create backend job for Star Upload: {exc}", log_box, log_file='star_log.txt')
        messagebox.showerror('Secure Server Error', f'Could not create backend job:\n{exc}')
        return


def star_csv_start_app(csv_entry, star_email_entry, star_password_entry, lab_id_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, star_start_btn, star_csv_btn, stop_btn, stop_event):
    log("Checking Star CSV settings...", log_box, log_file='star_log.txt')
    csv_file = csv_entry.get().strip()
    email = star_email_entry.get().strip()
    pwd = star_password_entry.get().strip()
    lab_id = lab_id_entry.get().strip() or '1'
    interval_value = interval_entry.get().strip() or '5'

    csv_valid = csv_file and (os.path.isfile(csv_file) or os.path.isdir(csv_file))
    if not csv_valid:
        log("âŒ Error: Please provide a valid CSV/Excel file or folder.", log_box, log_file='star_log.txt')
        messagebox.showerror('Error', 'Please provide a valid CSV/Excel file or folder.')
        return

    if not email or not pwd:
        log("âŒ Error: Star Email and Password are required.", log_box, log_file='star_log.txt')
        messagebox.showerror('Error', 'Star Email and Password are required.')
        return

    try:
        interval = int(interval_value)
    except ValueError:
        interval = 5

    log(f"ðŸš€ Starting Star CSV Upload (Interval: {interval}s, Lab ID: {lab_id})...", log_box, log_file='star_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    star_start_btn.configure(state='disabled')
    star_csv_btn.configure(state='disabled')
    stop_btn.configure(state='normal')
    try:
        lab_id_value = int(lab_id) if str(lab_id).isdigit() else None
        start_backend_tracked_worker(
            channel='star',
            channel_label='Star CSV Upload',
            log_box=log_box,
            stop_event=stop_event,
            worker_func=star_worker,
            worker_args=('', csv_file, email, pwd, lab_id, interval, once_var.get(), log_box, stop_event),
            source_path=csv_file,
            source_file_name=os.path.basename(csv_file),
            file_paths=collect_star_job_files('', csv_file),
            notes=f'Star CSV upload session for lab {lab_id}.',
            lab_id=lab_id_value,
        )
    except Exception as exc:
        start_btn.configure(state='normal')
        gmail_start_btn.configure(state='normal')
        star_start_btn.configure(state='normal')
        star_csv_btn.configure(state='normal')
        stop_btn.configure(state='disabled')
        log(f"❌ Error: Could not create backend job for Star CSV Upload: {exc}", log_box, log_file='star_log.txt')
        messagebox.showerror('Secure Server Error', f'Could not create backend job:\n{exc}')
        return


def whatsapp_csv_start_app(folder_entry, csv_entry, whatsapp_secret_entry, whatsapp_account_entry, log_box, start_btn, gmail_start_btn, whatsapp_start_btn, whatsapp_csv_btn, stop_btn, stop_event):
    log("Checking WhatsApp CSV settings...", log_box, log_file='whatsapp_log.txt')
    folder = folder_entry.get().strip()
    csv_file = csv_entry.get().strip()
    secret = whatsapp_secret_entry.get().strip()
    account = whatsapp_account_entry.get().strip()

    if not folder or not os.path.isdir(folder):
        log("❌ Error: Invalid PDF Folder path.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path.')
        return
    if not csv_file or not os.path.isfile(csv_file):
        log("❌ Error: Valid CSV file required.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'Please provide a valid CSV file path.')
        return
    if not secret or not account:
        log("❌ Error: WhatsApp Secret and Account are required.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'WhatsApp Secret and Account ID are required.')
        return

    log(f"🚀 Starting WhatsApp CSV Batch...", log_box, log_file='whatsapp_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    whatsapp_start_btn.configure(state='disabled')
    whatsapp_csv_btn.configure(state='disabled')
    stop_btn.configure(state='normal')
    try:
        start_backend_tracked_worker(
            channel='whatsapp',
            channel_label='WhatsApp CSV Batch',
            log_box=log_box,
            stop_event=stop_event,
            worker_func=csv_whatsapp_worker,
            worker_args=(csv_file, folder, secret, account, log_box, stop_event),
            source_path=csv_file,
            source_file_name=os.path.basename(csv_file),
            file_paths=collect_csv_attachment_paths(csv_file, folder),
            notes='CSV-driven WhatsApp batch session.',
        )
    except Exception as exc:
        start_btn.configure(state='normal')
        gmail_start_btn.configure(state='normal')
        whatsapp_start_btn.configure(state='normal')
        whatsapp_csv_btn.configure(state='normal')
        stop_btn.configure(state='disabled')
        log(f"❌ Error: Could not create backend job for WhatsApp CSV Batch: {exc}", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Secure Server Error', f'Could not create backend job:\n{exc}')
        return


def gmail_start_app(folder_entry, gmail_sender_entry, gmail_password_entry, recipient_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, stop_btn, stop_event):
    folder = folder_entry.get().strip()
    gmail_sender = gmail_sender_entry.get().strip()
    gmail_pwd = gmail_password_entry.get().strip()
    recipient = recipient_entry.get().strip()
    interval_value = interval_entry.get().strip() or '2'

    if not folder or not os.path.isdir(folder):
        log("Error: invalid or empty PDF folder path.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path.')
        return
    if not recipient:
        log("Error: recipient email is required.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Recipient email is required.')
        return

    try:
        interval = int(interval_value)
        if interval <= 0:
            raise ValueError
    except ValueError:
        log("Error: interval must be a positive integer.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Interval must be a positive integer number of seconds.')
        return

    log(f"Starting Gmail Upload (interval {interval}s)...", log_box, log_file='gmail_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def run_gmail_upload():
        try:
            gmail_worker(folder, gmail_sender, gmail_pwd, recipient, interval, once_var.get(), log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn)

    threading.Thread(target=run_gmail_upload, daemon=True).start()


def csv_start_app(folder_entry, csv_entry, gmail_sender_entry, gmail_password_entry, log_box, start_btn, gmail_start_btn, csv_start_btn, stop_btn, stop_event):
    folder = folder_entry.get().strip()
    csv_file = csv_entry.get().strip()
    gmail_sender = gmail_sender_entry.get().strip()
    gmail_pwd = gmail_password_entry.get().strip()

    if not folder or not os.path.isdir(folder):
        log("Error: invalid folder path for CSV attachments.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path for attachments.')
        return
    if not csv_file or not os.path.isfile(csv_file):
        log("Error: valid CSV file is required.", log_box, log_file='gmail_log.txt')
        messagebox.showerror('Error', 'Please provide a valid CSV file path.')
        return

    log("Starting Gmail CSV Batch...", log_box, log_file='gmail_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    csv_start_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def run_csv_gmail_upload():
        try:
            csv_gmail_worker(csv_file, folder, gmail_sender, gmail_pwd, log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn, csv_start_btn)

    threading.Thread(target=run_csv_gmail_upload, daemon=True).start()


def whatsapp_start_app(folder_entry, whatsapp_recipient_entry, whatsapp_message_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, whatsapp_start_btn, stop_btn, stop_event):
    folder = folder_entry.get().strip()
    recipient = whatsapp_recipient_entry.get().strip()
    message = whatsapp_message_entry.get().strip()
    interval_value = interval_entry.get().strip() or '2'

    if not folder or not os.path.isdir(folder):
        log("Error: invalid or empty PDF folder path.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path.')
        return
    if not recipient:
        log("Error: WhatsApp recipient is required.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'WhatsApp recipient is required.')
        return

    try:
        interval = int(interval_value)
        if interval <= 0:
            raise ValueError
    except ValueError:
        interval = 2

    log(f"Starting WhatsApp Upload (interval {interval}s)...", log_box, log_file='whatsapp_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    whatsapp_start_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def run_whatsapp_upload():
        try:
            whatsapp_worker(folder, WALOCAL_AUTHKEY, WALOCAL_TEMPID, recipient, message, interval, once_var.get(), log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn, whatsapp_start_btn)

    threading.Thread(target=run_whatsapp_upload, daemon=True).start()


def star_start_app(folder_entry, csv_entry, star_email_entry, star_password_entry, lab_id_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, star_start_btn, star_csv_btn, stop_btn, stop_event):
    folder = folder_entry.get().strip()
    csv_file = csv_entry.get().strip()
    email = star_email_entry.get().strip()
    pwd = star_password_entry.get().strip()
    lab_id = lab_id_entry.get().strip() or '1'
    interval_value = interval_entry.get().strip() or '5'

    folder_valid = folder and os.path.isdir(folder)
    csv_valid = csv_file and (os.path.isfile(csv_file) or os.path.isdir(csv_file))
    if not folder_valid and not csv_valid:
        log("Error: provide a valid PDF folder or CSV directory/file.", log_box, log_file='star_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path or a specific CSV directory/file.')
        return

    try:
        interval = int(interval_value)
        if interval <= 0:
            raise ValueError
    except ValueError:
        interval = 5

    log(f"Starting Star Upload (interval {interval}s, lab {lab_id})...", log_box, log_file='star_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    star_start_btn.configure(state='disabled')
    star_csv_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def run_star_upload():
        try:
            star_worker(folder, csv_file, email, pwd, lab_id, interval, once_var.get(), log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn, star_start_btn, star_csv_btn)

    threading.Thread(target=run_star_upload, daemon=True).start()


def star_csv_start_app(csv_entry, star_email_entry, star_password_entry, lab_id_entry, interval_entry, once_var, log_box, start_btn, gmail_start_btn, star_start_btn, star_csv_btn, stop_btn, stop_event):
    csv_file = csv_entry.get().strip()
    email = star_email_entry.get().strip()
    pwd = star_password_entry.get().strip()
    lab_id = lab_id_entry.get().strip() or '1'
    interval_value = interval_entry.get().strip() or '5'

    csv_valid = csv_file and (os.path.isfile(csv_file) or os.path.isdir(csv_file))
    if not csv_valid:
        log("Error: provide a valid CSV or Excel file/folder.", log_box, log_file='star_log.txt')
        messagebox.showerror('Error', 'Please provide a valid CSV/Excel file or folder.')
        return

    try:
        interval = int(interval_value)
        if interval <= 0:
            raise ValueError
    except ValueError:
        interval = 5

    log(f"Starting Star CSV Upload (interval {interval}s, lab {lab_id})...", log_box, log_file='star_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    star_start_btn.configure(state='disabled')
    star_csv_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def run_star_csv_upload():
        try:
            star_worker('', csv_file, email, pwd, lab_id, interval, once_var.get(), log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn, star_start_btn, star_csv_btn)

    threading.Thread(target=run_star_csv_upload, daemon=True).start()


def healhub_bulk_start_app(
    folder_entry,
    mapping_entry,
    healhub_email_entry,
    healhub_password_entry,
    healhub_lab_entry,
    log_box,
    start_btn,
    gmail_start_btn,
    star_start_btn,
    star_csv_btn,
    healhub_bulk_btn,
    stop_btn,
    stop_event,
):
    folder = folder_entry.get().strip()
    mapping_file = mapping_entry.get().strip()
    email = healhub_email_entry.get().strip()
    password = healhub_password_entry.get().strip()
    lab_value = healhub_lab_entry.get().strip()

    if not folder or not os.path.isdir(folder):
        log('❌ HealHub bulk upload needs a valid PDF folder.', log_box, log_file='healhub_bulk_log.txt')
        messagebox.showerror('Error', 'Please provide a valid PDF folder for HealHub bulk upload.')
        return
    if not mapping_file or not os.path.isfile(mapping_file):
        log('❌ HealHub bulk upload needs a valid CSV/XLSX mapping file.', log_box, log_file='healhub_bulk_log.txt')
        messagebox.showerror('Error', 'Please provide a valid CSV/XLSX mapping file.')
        return
    if not email or not password:
        log('❌ HealHub email and password are required.', log_box, log_file='healhub_bulk_log.txt')
        messagebox.showerror('Error', 'HealHub email and password are required.')
        return
    if not lab_value:
        log('❌ HealHub laboratory value is required.', log_box, log_file='healhub_bulk_log.txt')
        messagebox.showerror('Error', 'Please enter the HealHub lab id, unique id, or name.')
        return

    log('Starting HealHub bulk upload...', log_box, log_file='healhub_bulk_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    star_start_btn.configure(state='disabled')
    star_csv_btn.configure(state='disabled')
    healhub_bulk_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def run_healhub_bulk_upload():
        try:
            healhub_bulk_upload_worker(folder, mapping_file, email, password, lab_value, log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn, star_start_btn, star_csv_btn, healhub_bulk_btn)

    threading.Thread(target=run_healhub_bulk_upload, daemon=True).start()


def whatsapp_csv_start_app(folder_entry, csv_entry, log_box, start_btn, gmail_start_btn, whatsapp_start_btn, whatsapp_csv_btn, stop_btn, stop_event):
    folder = folder_entry.get().strip()
    csv_file = csv_entry.get().strip()

    if not folder or not os.path.isdir(folder):
        log("Error: invalid folder path for WhatsApp CSV batch.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'Please provide a valid folder path.')
        return
    if not csv_file or not os.path.isfile(csv_file):
        log("Error: valid CSV file required for WhatsApp batch.", log_box, log_file='whatsapp_log.txt')
        messagebox.showerror('Error', 'Please provide a valid CSV file path.')
        return

    log("Starting WhatsApp CSV Batch...", log_box, log_file='whatsapp_log.txt')
    stop_event.clear()
    start_btn.configure(state='disabled')
    gmail_start_btn.configure(state='disabled')
    whatsapp_start_btn.configure(state='disabled')
    whatsapp_csv_btn.configure(state='disabled')
    stop_btn.configure(state='normal')

    def run_whatsapp_csv_upload():
        try:
            csv_whatsapp_worker(csv_file, folder, WALOCAL_AUTHKEY, WALOCAL_TEMPID, log_box, stop_event)
        finally:
            restore_button_states(stop_btn, start_btn, gmail_start_btn, whatsapp_start_btn, whatsapp_csv_btn)

    threading.Thread(target=run_whatsapp_csv_upload, daemon=True).start()


def build_gui():
    root = tk.Tk()
    root.title('PDF Batch Uploader - Voice Controlled (Star Edition)')
    root.geometry('920x720')

    frame = tk.Frame(root, padx=12, pady=12)
    frame.pack(fill='both', expand=True)

    path_frame = tk.Frame(frame)
    path_frame.grid(row=0, column=0, columnspan=3, sticky='w', pady=(0, 10))

    tk.Label(path_frame, text='PDF Folder:').grid(row=0, column=0, sticky='w')
    folder_entry = tk.Entry(path_frame, width=50)
    folder_entry.grid(row=0, column=1, padx=4, pady=2, sticky='w')
    tk.Button(path_frame, text='Browse', command=lambda: browse_folder(folder_entry)).grid(row=0, column=2, padx=4, pady=2)

    tk.Label(path_frame, text='CSV List:').grid(row=1, column=0, sticky='w')
    csv_entry = tk.Entry(path_frame, width=50)
    csv_entry.insert(0, r'G:\csv')
    csv_entry.grid(row=1, column=1, padx=4, pady=2, sticky='w')
    tk.Button(path_frame, text='Browse', command=lambda: browse_csv(csv_entry)).grid(row=1, column=2, padx=4, pady=2)

    # --- Website API upload section ---
    tk.Label(frame, text='Interval (seconds):').grid(row=1, column=0, sticky='w')
    interval_entry = tk.Entry(frame, width=20)
    interval_entry.insert(0, '2')
    interval_entry.grid(row=1, column=1, padx=4, pady=4, sticky='w')

    # Shared Recognizer instance for buttons
    recognizer_var = tk.StringVar(value='online')

    tk.Label(frame, text='Hospital Name:').grid(row=2, column=0, sticky='w')
    hospital_entry = tk.Entry(frame, width=40)
    hospital_entry.grid(row=2, column=1, padx=4, pady=4, sticky='w')
    tk.Button(frame, text='🎤', command=lambda: voice_fill_hospital(hospital_entry, log_box, None, recognizer_var.get() == 'online')).grid(row=2, column=2, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='Password:').grid(row=3, column=0, sticky='w')
    password_entry = tk.Entry(frame, width=40, show='*')
    password_entry.grid(row=3, column=1, padx=4, pady=4, sticky='w')
    tk.Button(frame, text='🎤', command=lambda: voice_fill_password(password_entry, log_box, None, recognizer_var.get() == 'online')).grid(row=3, column=2, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='Recognizer:').grid(row=4, column=0, sticky='w', padx=4)
    tk.OptionMenu(frame, recognizer_var, 'online', 'offline').grid(row=4, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='Key-Value Pairs (JSON):').grid(row=5, column=0, sticky='w')
    kvp_entry = tk.Entry(frame, width=60)
    kvp_entry.insert(0, '{}')
    kvp_entry.grid(row=5, column=1, padx=4, pady=4, sticky='w')

    # --- Gmail section ---
    tk.Label(frame, text='Gmail Sender:').grid(row=6, column=0, sticky='w')
    gmail_sender_entry = tk.Entry(frame, width=40)
    gmail_sender_entry.grid(row=6, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='Gmail App Password:').grid(row=7, column=0, sticky='w')
    gmail_password_entry = tk.Entry(frame, width=40, show='*')
    gmail_password_entry.grid(row=7, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='Recipient Email:').grid(row=8, column=0, sticky='w')
    recipient_entry = tk.Entry(frame, width=40)
    recipient_entry.grid(row=8, column=1, padx=4, pady=4, sticky='w')

    # --- WhatsApp section ---
    tk.Label(frame, text='WhatsApp Auth Key:', fg='green').grid(row=9, column=0, sticky='w')
    whatsapp_secret_entry = tk.Entry(frame, width=40, show='*')
    whatsapp_secret_entry.grid(row=9, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='WhatsApp Template ID:', fg='green').grid(row=10, column=0, sticky='w')
    whatsapp_account_entry = tk.Entry(frame, width=40)
    whatsapp_account_entry.grid(row=10, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='WhatsApp Receiver Number:', fg='green').grid(row=11, column=0, sticky='w')
    whatsapp_recipient_entry = tk.Entry(frame, width=40)
    whatsapp_recipient_entry.insert(0, '918619836715')
    whatsapp_recipient_entry.grid(row=11, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='WhatsApp ph1 (Patient):', fg='green').grid(row=12, column=0, sticky='w')
    whatsapp_message_entry = tk.Entry(frame, width=60)
    whatsapp_message_entry.insert(0, 'Patient Name')
    whatsapp_message_entry.grid(row=12, column=1, padx=4, pady=4, sticky='w')

    # --- Star Software Solution section ---
    tk.Label(frame, text='Star Email:', fg='blue').grid(row=13, column=0, sticky='w')
    star_email_entry = tk.Entry(frame, width=40)
    star_email_entry.grid(row=13, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='Star Password:', fg='blue').grid(row=14, column=0, sticky='w')
    star_password_entry = tk.Entry(frame, width=40, show='*')
    star_password_entry.grid(row=14, column=1, padx=4, pady=4, sticky='w')

    tk.Label(frame, text='Star Laboratory ID:', fg='blue').grid(row=15, column=0, sticky='w')
    star_lab_id_entry = tk.Entry(frame, width=10)
    star_lab_id_entry.insert(0, '1')
    star_lab_id_entry.grid(row=15, column=1, padx=4, pady=4, sticky='w')

    once_var = tk.BooleanVar(value=False)
    once_cb = tk.Checkbutton(frame, text='Run once (single cycle)', variable=once_var)
    once_cb.grid(row=16, column=1, sticky='w')

    start_btn = tk.Button(frame, text='Start API Upload', width=14)
    start_gmail_btn = tk.Button(frame, text='Start Gmail Upload', width=16)
    whatsapp_start_btn = tk.Button(frame, text='Start WhatsApp', width=14, bg='lightgreen')
    star_start_btn = tk.Button(frame, text='Start Star Upload', width=14, bg='lightblue')
    star_csv_btn = tk.Button(frame, text='Start Star CSV Upload', width=18, bg='lightblue')
    start_csv_btn = tk.Button(frame, text='CSV Email Batch', width=14)
    whatsapp_csv_btn = tk.Button(frame, text='CSV WhatsApp', width=14, bg='lightgreen')
    stop_btn = tk.Button(frame, text='Stop Upload', width=14, state='disabled')
    clear_btn = tk.Button(frame, text='Clear Uploaded List', width=18, command=lambda: clear_uploaded_list(log_box))
    
    # Voice command toggle button
    voice_active_event = threading.Event()
    voice_btn = tk.Button(frame, text='🎙️ Enable Voice', width=14, bg='lightgreen')
    
    def toggle_voice():
        if voice_active_event.is_set():
            voice_active_event.clear()
            voice_btn.config(text='🎙️ Enable Voice', bg='lightcoral')
            log("Voice commands DISABLED", log_box)
        else:
            voice_active_event.set()
            voice_btn.config(text='🎙️ Listening...', bg='lightgreen')
            log("Voice commands ENABLED - listening for commands...", log_box)
    
    voice_btn.configure(command=toggle_voice)

    stop_event = threading.Event()
    global_stop_event = threading.Event()

    start_btn.configure(command=lambda: start_app(folder_entry, hospital_entry, password_entry, kvp_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, stop_btn, global_stop_event))
    start_gmail_btn.configure(command=lambda: gmail_start_app(folder_entry, gmail_sender_entry, gmail_password_entry, recipient_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, stop_btn, global_stop_event))
    whatsapp_start_btn.configure(command=lambda: whatsapp_start_app(folder_entry, whatsapp_recipient_entry, whatsapp_message_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, whatsapp_start_btn, stop_btn, global_stop_event))
    star_start_btn.configure(command=lambda: star_start_app(folder_entry, csv_entry, star_email_entry, star_password_entry, star_lab_id_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, star_start_btn, star_csv_btn, stop_btn, global_stop_event))
    star_csv_btn.configure(command=lambda: star_csv_start_app(csv_entry, star_email_entry, star_password_entry, star_lab_id_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, star_start_btn, star_csv_btn, stop_btn, global_stop_event))
    start_csv_btn.configure(command=lambda: csv_start_app(folder_entry, csv_entry, gmail_sender_entry, gmail_password_entry, log_box, start_btn, start_gmail_btn, start_csv_btn, stop_btn, global_stop_event))
    whatsapp_csv_btn.configure(command=lambda: whatsapp_csv_start_app(folder_entry, csv_entry, log_box, start_btn, start_gmail_btn, whatsapp_start_btn, whatsapp_csv_btn, stop_btn, global_stop_event))
    
    def wrapped_stop():
        global_stop_event.set()
        start_btn.configure(state='normal')
        start_gmail_btn.configure(state='normal')
        whatsapp_start_btn.configure(state='normal')
        star_start_btn.configure(state='normal')
        star_csv_btn.configure(state='normal')
        start_csv_btn.configure(state='normal')
        whatsapp_csv_btn.configure(state='normal')
        stop_btn.configure(state='disabled')
        
    stop_btn.configure(command=wrapped_stop)

    start_btn.grid(row=17, column=0, sticky='w', pady=6)
    start_gmail_btn.grid(row=17, column=1, sticky='w', pady=6)
    whatsapp_start_btn.grid(row=17, column=2, sticky='w', pady=6)
    star_start_btn.grid(row=17, column=3, sticky='w', pady=6)
    start_csv_btn.grid(row=18, column=0, sticky='w', pady=6)
    whatsapp_csv_btn.grid(row=18, column=1, sticky='w', pady=6)
    star_csv_btn.grid(row=18, column=2, sticky='w', pady=6)
    stop_btn.grid(row=18, column=3, sticky='w', pady=6)
    voice_btn.grid(row=18, column=4, sticky='w', pady=6)
    clear_btn.grid(row=18, column=5, sticky='e', pady=6)

    tk.Label(frame, text='Log:').grid(row=19, column=0, sticky='nw')
    log_box = scrolledtext.ScrolledText(frame, width=105, height=18, state='disabled')
    log_box.grid(row=19, column=1, columnspan=5, pady=4)

    # Start voice command listener in background
    voice_cmd_thread = threading.Thread(target=voice_command_listener, args=(root, log_box, stop_event, voice_active_event,
                                                                                start_btn, start_gmail_btn, start_csv_btn, whatsapp_start_btn, whatsapp_csv_btn, star_start_btn, stop_btn,
                                                                                folder_entry, hospital_entry, password_entry, kvp_entry, interval_entry, once_var,
                                                                                gmail_sender_entry, gmail_password_entry, recipient_entry, csv_entry,
                                                                                whatsapp_recipient_entry, whatsapp_message_entry, 
                                                                                star_email_entry, star_password_entry, star_lab_id_entry, global_stop_event), daemon=True)
    voice_cmd_thread.start()
    log(f"✓ Voice listener ready. Click '🎙️ Enable Voice' to start listening for commands.", log_box)

    root.mainloop()


def build_gui_v2():
    root = tk.Tk()
    root.title('PDF Batch Uploader - Voice Controlled (Star Edition)')
    root.geometry('1120x860')
    root.configure(bg='#f3f6fb')

    main_container = tk.Frame(root, padx=12, pady=12, bg='#f3f6fb')
    main_container.pack(fill='both', expand=True)

    header = create_brand_header(main_container)
    header.pack(fill='x', pady=(0, 14))

    # --- TAB SYSTEM ---
    notebook = ttk.Notebook(main_container)
    notebook.pack(fill='both', expand=True)

    def create_scrollable_tab(notebook_widget, title_text):
        container = tk.Frame(notebook_widget, bg='#f3f6fb')
        canvas = tk.Canvas(container, bg='#f3f6fb', highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg='#f3f6fb', padx=10, pady=10)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        
        def on_canvas_configure(e):
            canvas.itemconfig(canvas_window, width=e.width)

        canvas.bind("<Configure>", on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)
        
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            
        canvas.bind('<Enter>', lambda _: canvas.bind_all('<MouseWheel>', _on_mousewheel))
        canvas.bind('<Leave>', lambda _: canvas.unbind_all('<MouseWheel>'))
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        notebook_widget.add(container, text=title_text)
        return scrollable_frame

    tab_uploads = create_scrollable_tab(notebook, '  Upload Dash  ')
    tab_analyzer = create_scrollable_tab(notebook, '  PDF Analyzer  ')
    tab_abdm = create_scrollable_tab(notebook, '  ABDM Sandbox  ')
    tab_settings = create_scrollable_tab(notebook, '  Settings  ')

    # Assign 'upload_frame' to tab_uploads to keep existing grid code working with minimal changes
    upload_frame = tab_uploads

    # Load existing config for pre-filling
    config = load_desktop_api_config()

    # --- SETTINGS TAB CONTENT ---
    tk.Label(tab_settings, text='ABDM Sandbox Credentials', font=('Arial', 14, 'bold'), bg='#f3f6fb', fg='#403b97').grid(row=0, column=0, columnspan=2, pady=(0, 20), sticky='w')
    
    tk.Label(tab_settings, text='ABDM Client ID:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=1, column=0, sticky='w', pady=8)
    abdm_id_entry = tk.Entry(tab_settings, width=40)
    abdm_id_entry.insert(0, config.get('abdm_client_id', ''))
    abdm_id_entry.grid(row=1, column=1, padx=10, sticky='w')
    
    tk.Label(tab_settings, text='ABDM Client Secret:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=2, column=0, sticky='w', pady=8)
    abdm_secret_entry = tk.Entry(tab_settings, width=40, show='*')
    abdm_secret_entry.insert(0, config.get('abdm_client_secret', ''))
    abdm_secret_entry.grid(row=2, column=1, padx=10, sticky='w')

    tk.Button(tab_settings, text='Save ABDM Credentials', bg='#403b97', fg='white', width=20, 
            command=lambda: abdm_handle_save_settings(abdm_id_entry, abdm_secret_entry, log_box)).grid(row=3, column=1, pady=20, sticky='w')

    tk.Frame(tab_settings, height=2, bg='#cccccc').grid(row=4, column=0, columnspan=2, sticky='ew', pady=20)
    
    tk.Label(tab_settings, text='General API Settings', font=('Arial', 14, 'bold'), bg='#f3f6fb', fg='#333333').grid(row=5, column=0, columnspan=2, pady=(0, 10), sticky='w')
    tk.Label(tab_settings, text='Server URL:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=6, column=0, sticky='w', pady=8)
    # Using an entry for URL if needed, though login handles it usually
    tk.Label(tab_settings, text=config.get('api_base_url'), bg='#f3f6fb', fg='blue').grid(row=6, column=1, padx=10, sticky='w')


    # --- PDF ANALYZER TAB CONTENT ---
    analyzer_header = tk.Label(
        tab_analyzer,
        text='PDF Analyzer',
        font=('Arial', 18, 'bold'),
        bg='#f3f6fb',
        fg='#2c3e50',
    )
    analyzer_header.pack(anchor='w', pady=(0, 8))

    analyzer_hint = tk.Label(
        tab_analyzer,
        text='Analyze a single PDF or a folder of PDFs and sync extracted patient data into an XLSX or CSV database.',
        bg='#f3f6fb',
        fg='#555555',
        font=('Arial', 10, 'italic'),
        wraplength=900,
        justify='left',
    )
    analyzer_hint.pack(anchor='w', pady=(0, 12))

    analyzer_frame = tk.LabelFrame(
        tab_analyzer,
        text=' Analyzer Setup ',
        padx=14,
        pady=14,
        bg='#f3f6fb',
        font=('Arial', 11, 'bold'),
    )
    analyzer_frame.pack(fill='x', pady=(0, 12))

    tk.Label(analyzer_frame, text='PDF File / Folder:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=0, column=0, sticky='w', pady=6)
    analyzer_pdf_entry = tk.Entry(analyzer_frame, width=70)
    analyzer_pdf_entry.grid(row=0, column=1, padx=6, pady=6, sticky='w')
    tk.Button(analyzer_frame, text='Browse File', command=lambda: browse_analyzer_pdf(analyzer_pdf_entry)).grid(row=0, column=2, padx=4, pady=6)
    tk.Button(analyzer_frame, text='Browse Folder', command=lambda: browse_analyzer_folder(analyzer_pdf_entry)).grid(row=0, column=3, padx=4, pady=6)

    tk.Label(analyzer_frame, text='Database (XLSX / CSV):', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=1, column=0, sticky='w', pady=6)
    analyzer_db_entry = tk.Entry(analyzer_frame, width=70)
    analyzer_db_entry.grid(row=1, column=1, padx=6, pady=6, sticky='w')
    tk.Button(analyzer_frame, text='Browse DB', command=lambda: browse_analyzer_db(analyzer_db_entry)).grid(row=1, column=2, padx=4, pady=6, sticky='w')

    analyzer_overwrite_var = tk.BooleanVar(value=False)
    tk.Checkbutton(
        analyzer_frame,
        text='Force overwrite existing values',
        variable=analyzer_overwrite_var,
        bg='#f3f6fb',
    ).grid(row=2, column=1, columnspan=2, sticky='w', pady=(10, 0))

    analyzer_process_btn = tk.Button(analyzer_frame, text='Process & Sync Database', width=24, bg='#27ae60', fg='white')
    analyzer_process_btn.grid(row=3, column=1, sticky='w', pady=(14, 0))

    analyzer_status_lbl = tk.Label(analyzer_frame, text='Ready for analysis.', bg='#f3f6fb', fg='#2f5d50')
    analyzer_status_lbl.grid(row=3, column=2, columnspan=2, sticky='w', pady=(14, 0))

    analyzer_log_frame = tk.LabelFrame(
        tab_analyzer,
        text=' Analyzer Logs ',
        padx=10,
        pady=10,
        bg='#f3f6fb',
        font=('Arial', 11, 'bold'),
    )
    analyzer_log_frame.pack(fill='both', expand=True)

    analyzer_log_box = scrolledtext.ScrolledText(analyzer_log_frame, width=118, height=18, state='disabled', wrap='word')
    analyzer_log_box.pack(fill='both', expand=True)

    def browse_analyzer_pdf(target_entry):
        file_path = filedialog.askopenfilename(
            title='Open PDF',
            filetypes=[('PDF Reports', '*.pdf'), ('All Files', '*.*')],
        )
        if file_path:
            target_entry.delete(0, 'end')
            target_entry.insert(0, file_path)

    def browse_analyzer_folder(target_entry):
        folder_path = filedialog.askdirectory(title='Select PDF Folder')
        if folder_path:
            target_entry.delete(0, 'end')
            target_entry.insert(0, folder_path)

    def browse_analyzer_db(target_entry):
        file_path = filedialog.askopenfilename(
            title='Select Database',
            filetypes=[('Database Files', '*.xlsx *.csv'), ('Excel Workbook', '*.xlsx'), ('CSV File', '*.csv')],
        )
        if file_path:
            target_entry.delete(0, 'end')
            target_entry.insert(0, file_path)

    def collect_analyzer_pdf_files(path_value):
        if os.path.isfile(path_value):
            if path_value.lower().endswith('.pdf'):
                return [path_value]
            return []
        if os.path.isdir(path_value):
            pdf_files = []
            for root_dir, _dirs, files in os.walk(path_value):
                for file_name in sorted(files):
                    if file_name.lower().endswith('.pdf'):
                        pdf_files.append(os.path.join(root_dir, file_name))
            return pdf_files
        return []

    def run_pdf_analyzer():
        pdf_path = analyzer_pdf_entry.get().strip()
        db_path = analyzer_db_entry.get().strip()

        if not pdf_path or not db_path:
            messagebox.showwarning('Input Error', 'Please provide both PDF and Database paths.')
            return

        if pdfplumber is None:
            messagebox.showerror('Missing Dependency', 'pdfplumber is not installed, so PDF analysis cannot run.')
            return

        if not os.path.exists(pdf_path):
            log(f'Path not found: {pdf_path}', analyzer_log_box, log_file='pdf_analyzer_log.txt')
            messagebox.showerror('Path Error', f'Path not found:\n{pdf_path}')
            return

        pdf_files = collect_analyzer_pdf_files(pdf_path)
        if not pdf_files:
            log(f'No PDF files found at: {pdf_path}', analyzer_log_box, log_file='pdf_analyzer_log.txt')
            messagebox.showwarning('No PDFs', 'No PDF files were found at the given path.')
            return

        analyzer_process_btn.configure(state='disabled')
        analyzer_status_lbl.configure(text='Processing...', fg='#b36b00')

        extractor = ReportExtractor()
        handler = DatabaseHandler(db_path)
        overwrite = analyzer_overwrite_var.get()

        def worker_thread():
            total = len(pdf_files)
            success_count = 0
            fail_count = 0
            results_summary = []

            try:
                if os.path.isdir(pdf_path):
                    log(f"Folder mode: Found {total} PDF file(s) in '{pdf_path}'", analyzer_log_box, log_file='pdf_analyzer_log.txt')
                else:
                    log(f'Single file mode: {os.path.basename(pdf_path)}', analyzer_log_box, log_file='pdf_analyzer_log.txt')

                for idx, file_path in enumerate(pdf_files, start=1):
                    pdf_filename = os.path.basename(file_path)
                    log(f'[{idx}/{total}] Processing: {pdf_filename}', analyzer_log_box, log_file='pdf_analyzer_log.txt')

                    try:
                        data = extractor.extract(file_path)
                        log(
                            f"  Data -> ID: {data['uhid']}, Name: {data['name']}, Mobile: {data['mobile']}",
                            analyzer_log_box,
                            log_file='pdf_analyzer_log.txt',
                        )

                        if not data['uhid']:
                            raise Exception('Could not find Unique Id / UHID / ID in the PDF report.')

                        action = handler.update_record(data, pdf_filename, overwrite=overwrite)
                        current_date = datetime.now().strftime('%d/%m/%Y')
                        log(f'  Report PDF: {pdf_filename} | Date: {current_date}', analyzer_log_box, log_file='pdf_analyzer_log.txt')
                        log(f'  Result: {action}', analyzer_log_box, log_file='pdf_analyzer_log.txt')
                        success_count += 1
                        results_summary.append(f'✔ {pdf_filename}: {action}')
                    except Exception as exc:
                        log(f'  Failed: {str(exc)}', analyzer_log_box, log_file='pdf_analyzer_log.txt')
                        fail_count += 1
                        results_summary.append(f'✘ {pdf_filename}: {str(exc)}')

                log('=' * 50, analyzer_log_box, log_file='pdf_analyzer_log.txt')
                log(
                    f'Processing complete - {success_count} succeeded, {fail_count} failed out of {total} PDF(s).',
                    analyzer_log_box,
                    log_file='pdf_analyzer_log.txt',
                )

                summary_text = '\n'.join(results_summary)
                root.after(
                    0,
                    lambda: messagebox.showinfo(
                        'Processing Complete',
                        f'Total PDFs: {total}\n'
                        f'Succeeded: {success_count}\n'
                        f'Failed: {fail_count}\n\n'
                        f'{summary_text}',
                    ),
                )
            finally:
                root.after(0, lambda: analyzer_process_btn.configure(state='normal'))
                root.after(0, lambda: analyzer_status_lbl.configure(text='Ready for analysis.', fg='#2f5d50'))

        threading.Thread(target=worker_thread, daemon=True).start()

    analyzer_process_btn.configure(command=run_pdf_analyzer)


    # --- ABDM SANDBOX TAB CONTENT ---
    # Milestone 1: ABHA
    m1_frame = tk.LabelFrame(tab_abdm, text=" Milestone 1: ABHA Registration (Mock) ", padx=15, pady=15, bg='#f3f6fb', font=('Arial', 11, 'bold'))
    m1_frame.pack(fill='x', pady=10)
    
    tk.Label(m1_frame, text="In Milestone 1, we identify the patient by creating or verifying their ABHA ID.", bg='#f3f6fb', wraplength=500, justify='left').grid(row=0, column=0, columnspan=3, pady=(0, 10), sticky='w')
    
    tk.Label(m1_frame, text="Mobile / Aadhaar:", bg='#f3f6fb', font=('Arial', 10, 'bold')).grid(row=1, column=0, sticky='w')
    abdm_mobile_entry = tk.Entry(m1_frame, width=25)
    abdm_mobile_entry.grid(row=1, column=1, padx=5, sticky='w')
    
    tk.Button(m1_frame, text="Generate OTP", command=lambda: abdm_mock_generate_otp(abdm_mobile_entry, log_box)).grid(row=1, column=2, padx=5)
    
    tk.Label(m1_frame, text="Enter OTP:", bg='#f3f6fb', font=('Arial', 10, 'bold')).grid(row=2, column=0, sticky='w', pady=10)
    abdm_otp_entry = tk.Entry(m1_frame, width=15)
    abdm_otp_entry.grid(row=2, column=1, padx=5, sticky='w')
    
    tk.Button(m1_frame, text="Verify & Create ABHA", bg='lightgreen', 
            command=lambda: abdm_mock_verify_otp(abdm_mobile_entry, abdm_otp_entry, log_box)).grid(row=2, column=2, padx=5)

    # Milestone 2: HIP
    m2_frame = tk.LabelFrame(tab_abdm, text=" Milestone 2: HIP Record Linking (Mock) ", padx=15, pady=15, bg='#f3f6fb', font=('Arial', 11, 'bold'))
    m2_frame.pack(fill='x', pady=10)
    
    tk.Label(m2_frame, text="In Milestone 2, you link your hospital's health records to the patient's ABHA.", bg='#f3f6fb', wraplength=500, justify='left').grid(row=0, column=0, columnspan=3, pady=(0, 10), sticky='w')
    
    tk.Label(m2_frame, text="Active ABHA:", bg='#f3f6fb', font=('Arial', 10, 'bold')).grid(row=1, column=0, sticky='w')
    active_abha_lbl = tk.Label(m2_frame, text="None Linked", fg='red', bg='#f3f6fb')
    active_abha_lbl.grid(row=1, column=1, sticky='w')
    
    tk.Button(m2_frame, text="Simulate Discovery & Linking", state='disabled').grid(row=1, column=2, padx=5)

    tk.Label(tab_abdm, text="💡 Tip: To start a real test, obtain your credentials from the portal and enter them in the 'Settings' tab.", 
            bg='#f3f6fb', fg='#555555', font=('Arial', 9, 'italic')).pack(side='bottom', pady=10, anchor='w')




    path_frame = tk.Frame(upload_frame, bg='#f3f6fb')
    path_frame.grid(row=0, column=0, columnspan=4, sticky='w', pady=(0, 10))

    tk.Label(path_frame, text='PDF Folder:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=0, column=0, sticky='w')
    folder_entry = tk.Entry(path_frame, width=50)
    folder_entry.grid(row=0, column=1, padx=4, pady=2, sticky='w')
    tk.Button(path_frame, text='Browse', command=lambda: browse_folder(folder_entry)).grid(row=0, column=2, padx=4, pady=2)

    tk.Label(path_frame, text='CSV List:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=1, column=0, sticky='w')
    csv_entry = tk.Entry(path_frame, width=50)
    csv_entry.insert(0, r'G:\csv')
    csv_entry.grid(row=1, column=1, padx=4, pady=2, sticky='w')
    tk.Button(path_frame, text='Browse', command=lambda: browse_csv(csv_entry)).grid(row=1, column=2, padx=4, pady=2)

    tk.Label(path_frame, text='Mode:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=0, column=3, padx=(20, 4), sticky='w')
    tk.Label(path_frame, text='Offline', bg='#f3f6fb', fg='#403b97', font=('Arial', 10)).grid(row=0, column=4, sticky='w')
    tk.Label(path_frame, text='Status:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=1, column=3, padx=(20, 4), sticky='w')
    tk.Label(path_frame, text='Ready', bg='#f3f6fb', fg='#2f5d50', font=('Arial', 10, 'bold')).grid(row=1, column=4, sticky='w')


    recognizer_var = tk.StringVar(value='online')
    label_specs = [
        ('Interval (seconds):', 2, '#202020'),
        ('Hospital Name:', 3, '#202020'),
        ('Password:', 4, '#202020'),
        ('Recognizer:', 5, '#202020'),
        ('Key-Value Pairs (JSON):', 6, '#202020'),
        ('Gmail Sender:', 7, '#202020'),
        ('Gmail App Password:', 8, '#202020'),
        ('Recipient Email:', 9, '#202020'),
        ('WhatsApp Receiver Number:', 12, 'green'),
        ('WhatsApp ph1 (Patient):', 13, 'green'),
        ('Star Email:', 14, 'blue'),
        ('Star Password:', 15, 'blue'),
        ('Star Laboratory ID:', 16, 'blue'),
        ('HealHub Email:', 17, '#7a3d00'),
        ('HealHub Password:', 18, '#7a3d00'),
        ('HealHub Mapping File:', 19, '#7a3d00'),
        ('HealHub Lab:', 20, '#7a3d00'),
    ]
    for text, row, fg in label_specs:
        tk.Label(upload_frame, text=text, bg='#f3f6fb', fg=fg, font=('Arial', 11, 'bold')).grid(row=row, column=0, sticky='w')

    interval_entry = tk.Entry(upload_frame, width=20)
    interval_entry.insert(0, '2')
    interval_entry.grid(row=2, column=1, padx=4, pady=4, sticky='w')

    hospital_entry = tk.Entry(upload_frame, width=40)
    hospital_entry.grid(row=3, column=1, padx=4, pady=4, sticky='w')

    password_entry = tk.Entry(upload_frame, width=40, show='*')
    password_entry.grid(row=4, column=1, padx=4, pady=4, sticky='w')

    tk.OptionMenu(upload_frame, recognizer_var, 'online', 'offline').grid(row=5, column=1, padx=4, pady=4, sticky='w')
    recognizer_var.set('online')

    kvp_entry = tk.Entry(upload_frame, width=60)
    kvp_entry.insert(0, '{}')
    kvp_entry.grid(row=6, column=1, padx=4, pady=4, sticky='w')

    gmail_sender_entry = tk.Entry(upload_frame, width=40)
    gmail_sender_entry.grid(row=7, column=1, padx=4, pady=4, sticky='w')

    gmail_password_entry = tk.Entry(upload_frame, width=40, show='*')
    gmail_password_entry.grid(row=8, column=1, padx=4, pady=4, sticky='w')

    recipient_entry = tk.Entry(upload_frame, width=40)
    recipient_entry.grid(row=9, column=1, padx=4, pady=4, sticky='w')

    whatsapp_recipient_entry = tk.Entry(upload_frame, width=40)
    whatsapp_recipient_entry.insert(0, '918619836715')
    whatsapp_recipient_entry.grid(row=12, column=1, padx=4, pady=4, sticky='w')

    whatsapp_message_entry = tk.Entry(upload_frame, width=60)
    whatsapp_message_entry.insert(0, 'Patient Name')
    whatsapp_message_entry.grid(row=13, column=1, padx=4, pady=4, sticky='w')

    star_email_entry = tk.Entry(upload_frame, width=40)
    star_email_entry.grid(row=14, column=1, padx=4, pady=4, sticky='w')

    star_password_entry = tk.Entry(upload_frame, width=40, show='*')
    star_password_entry.grid(row=15, column=1, padx=4, pady=4, sticky='w')

    star_lab_id_entry = tk.Entry(upload_frame, width=10)
    star_lab_id_entry.insert(0, '1')
    star_lab_id_entry.grid(row=16, column=1, padx=4, pady=4, sticky='w')

    healhub_email_entry = tk.Entry(upload_frame, width=40)
    healhub_email_entry.grid(row=17, column=1, padx=4, pady=4, sticky='w')

    healhub_password_entry = tk.Entry(upload_frame, width=40, show='*')
    healhub_password_entry.grid(row=18, column=1, padx=4, pady=4, sticky='w')

    healhub_mapping_entry = tk.Entry(upload_frame, width=50)
    healhub_mapping_entry.grid(row=19, column=1, padx=4, pady=4, sticky='w')
    tk.Button(upload_frame, text='Browse', command=lambda: browse_csv(healhub_mapping_entry)).grid(row=19, column=2, padx=4, pady=4, sticky='w')

    healhub_lab_entry = tk.Entry(upload_frame, width=24)
    healhub_lab_entry.grid(row=20, column=1, padx=4, pady=4, sticky='w')


    log_frame = tk.Frame(main_container, bg='#f3f6fb', bd=1, relief='sunken')
    log_box = scrolledtext.ScrolledText(log_frame, width=118, height=14, state='disabled', wrap='word')
    log_box.pack(fill='both', expand=True)

    def _on_log_mousewheel(event):
        log_box.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        return 'break'

    log_box.bind('<MouseWheel>', _on_log_mousewheel)
    log_box.bind('<Button-4>', lambda event: (log_box.yview_scroll(-1, 'units'), 'break')[1])
    log_box.bind('<Button-5>', lambda event: (log_box.yview_scroll(1, 'units'), 'break')[1])
    tk.Button(upload_frame, text='Mic', command=lambda: voice_fill_hospital(hospital_entry, log_box, None, recognizer_var.get() == 'online')).grid(row=3, column=2, padx=4, pady=4, sticky='w')
    tk.Button(upload_frame, text='Mic', command=lambda: voice_fill_password(password_entry, log_box, None, recognizer_var.get() == 'online')).grid(row=4, column=2, padx=4, pady=4, sticky='w')

    once_var = tk.BooleanVar(value=False)
    tk.Checkbutton(upload_frame, text='Run once (single cycle)', variable=once_var, bg='#f3f6fb').grid(row=21, column=1, sticky='w')

    start_btn = tk.Button(upload_frame, text='Start API Upload', width=14)
    start_gmail_btn = tk.Button(upload_frame, text='Start Gmail Upload', width=16)
    whatsapp_start_btn = tk.Button(upload_frame, text='Start WhatsApp', width=14, bg='lightgreen')
    star_start_btn = tk.Button(upload_frame, text='Start Star Upload', width=14, bg='lightblue')
    star_csv_btn = tk.Button(upload_frame, text='Start Star CSV Upload', width=18, bg='lightblue')
    healhub_bulk_btn = tk.Button(upload_frame, text='Start HealHub Bulk', width=18, bg='#ffd69a')
    start_csv_btn = tk.Button(upload_frame, text='CSV Email Batch', width=14)
    whatsapp_csv_btn = tk.Button(upload_frame, text='CSV WhatsApp', width=14, bg='lightgreen')
    stop_btn = tk.Button(upload_frame, text='Stop Upload', width=14, state='disabled')
    clear_btn = tk.Button(upload_frame, text='Clear Uploaded List', width=18, command=lambda: clear_uploaded_list(log_box))

    voice_active_event = threading.Event()
    voice_btn = tk.Button(upload_frame, text='Enable Voice', width=14, bg='lightgreen')
    log_toggle_btn = tk.Button(upload_frame, text='Hide Log', width=12)


    def toggle_voice():
        if voice_active_event.is_set():
            voice_active_event.clear()
            voice_btn.config(text='Enable Voice', bg='lightcoral')
            log("Voice commands DISABLED", log_box)
        else:
            voice_active_event.set()
            voice_btn.config(text='Listening...', bg='lightgreen')
            log("Voice commands ENABLED - listening for commands...", log_box)

    voice_btn.configure(command=toggle_voice)

    log_visible = {'value': True}

    def toggle_log_view():
        if log_visible['value']:
            log_frame.grid_remove()
            log_toggle_btn.config(text='View Log')
            log_visible['value'] = False
        else:
            log_frame.grid()
            log_toggle_btn.config(text='Hide Log')
            log_visible['value'] = True

    log_toggle_btn.configure(command=toggle_log_view)

    stop_event = threading.Event()
    global_stop_event = threading.Event()

    start_btn.configure(command=lambda: start_app(folder_entry, hospital_entry, password_entry, kvp_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, stop_btn, global_stop_event))
    start_gmail_btn.configure(command=lambda: gmail_start_app(folder_entry, gmail_sender_entry, gmail_password_entry, recipient_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, stop_btn, global_stop_event))
    whatsapp_start_btn.configure(command=lambda: whatsapp_start_app(folder_entry, whatsapp_recipient_entry, whatsapp_message_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, whatsapp_start_btn, stop_btn, global_stop_event))
    star_start_btn.configure(command=lambda: star_start_app(folder_entry, csv_entry, star_email_entry, star_password_entry, star_lab_id_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, star_start_btn, star_csv_btn, stop_btn, global_stop_event))
    star_csv_btn.configure(command=lambda: star_csv_start_app(csv_entry, star_email_entry, star_password_entry, star_lab_id_entry, interval_entry, once_var, log_box, start_btn, start_gmail_btn, star_start_btn, star_csv_btn, stop_btn, global_stop_event))
    healhub_bulk_btn.configure(command=lambda: healhub_bulk_start_app(folder_entry, healhub_mapping_entry, healhub_email_entry, healhub_password_entry, healhub_lab_entry, log_box, start_btn, start_gmail_btn, star_start_btn, star_csv_btn, healhub_bulk_btn, stop_btn, global_stop_event))
    start_csv_btn.configure(command=lambda: csv_start_app(folder_entry, csv_entry, gmail_sender_entry, gmail_password_entry, log_box, start_btn, start_gmail_btn, start_csv_btn, stop_btn, global_stop_event))
    whatsapp_csv_btn.configure(command=lambda: whatsapp_csv_start_app(folder_entry, csv_entry, log_box, start_btn, start_gmail_btn, whatsapp_start_btn, whatsapp_csv_btn, stop_btn, global_stop_event))

    def wrapped_stop():
        global_stop_event.set()
        start_btn.configure(state='normal')
        start_gmail_btn.configure(state='normal')
        whatsapp_start_btn.configure(state='normal')
        star_start_btn.configure(state='normal')
        star_csv_btn.configure(state='normal')
        healhub_bulk_btn.configure(state='normal')
        start_csv_btn.configure(state='normal')
        whatsapp_csv_btn.configure(state='normal')
        stop_btn.configure(state='disabled')

    stop_btn.configure(command=wrapped_stop)

    start_btn.grid(row=22, column=0, sticky='w', pady=6)
    start_gmail_btn.grid(row=22, column=1, sticky='w', pady=6)
    whatsapp_start_btn.grid(row=22, column=2, sticky='w', pady=6)
    star_start_btn.grid(row=22, column=3, sticky='w', pady=6)
    start_csv_btn.grid(row=23, column=0, sticky='w', pady=6)
    whatsapp_csv_btn.grid(row=23, column=1, sticky='w', pady=6)
    star_csv_btn.grid(row=23, column=2, sticky='w', pady=6)
    healhub_bulk_btn.grid(row=23, column=3, sticky='w', pady=6)
    stop_btn.grid(row=23, column=4, sticky='w', pady=6)
    voice_btn.grid(row=23, column=5, sticky='w', pady=6)
    log_toggle_btn.grid(row=23, column=6, sticky='w', pady=6)
    clear_btn.grid(row=23, column=7, sticky='e', pady=6)

    tk.Label(upload_frame, text='Log Controller:', bg='#f3f6fb', font=('Arial', 11, 'bold')).grid(row=24, column=0, sticky='nw')
    # Use main_container for log placement to be persistent
    log_frame.pack(fill='both', expand=True, pady=(10, 0))

    main_container.grid_columnconfigure(1, weight=1)
    main_container.grid_rowconfigure(2, weight=1) # The notebook is row 1, log is pack()ed below it
    root.minsize(1120, 860)


    voice_cmd_thread = threading.Thread(target=voice_command_listener, args=(root, log_box, stop_event, voice_active_event,
                                                                                start_btn, start_gmail_btn, start_csv_btn, whatsapp_start_btn, whatsapp_csv_btn, star_start_btn, stop_btn,
                                                                                folder_entry, hospital_entry, password_entry, kvp_entry, interval_entry, once_var,
                                                                                gmail_sender_entry, gmail_password_entry, recipient_entry, csv_entry,
                                                                                whatsapp_recipient_entry, whatsapp_message_entry,
                                                                                star_email_entry, star_password_entry, star_lab_id_entry, global_stop_event), daemon=True)
    voice_cmd_thread.start()
    log("Voice listener ready. Click 'Enable Voice' to start listening for commands.", log_box)

    root.mainloop()



if __name__ == '__main__':
    if show_app_login():
        build_gui_v2()
    
