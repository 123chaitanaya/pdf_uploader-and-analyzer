from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from medical_monitor_app.data import store
from medical_monitor_app.schemas import DashboardPayload, PatientRecord, SimulationRequest


app = FastAPI(
    title="Medical Bed Monitor Demo",
    version="0.1.0",
    description="Standalone prototype for bedside patient monitoring and staff alerts.",
)


@app.get("/api/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok", "service": "medical-monitor-app"}


@app.get("/api/dashboard", response_model=DashboardPayload)
def get_dashboard() -> DashboardPayload:
    return store.dashboard_payload()


@app.get("/api/patients", response_model=list[PatientRecord])
def list_patients() -> list[PatientRecord]:
    return [store.get_patient(patient.id) for patient in store.list_summaries() if store.get_patient(patient.id) is not None]


@app.get("/api/patients/{patient_id}", response_model=PatientRecord)
def get_patient(patient_id: str) -> PatientRecord:
    patient = store.get_patient(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient


@app.post("/api/patients/{patient_id}/simulate", response_model=PatientRecord)
def simulate_patient(patient_id: str, payload: SimulationRequest) -> PatientRecord:
    patient = store.set_mode(patient_id, payload.mode)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return patient


@app.get("/", response_class=HTMLResponse)
def dashboard_page() -> str:
    return """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Medical Bed Monitor Demo</title>
  <style>
    :root {
      --bg: #eef3f1;
      --panel: rgba(255, 255, 255, 0.88);
      --panel-border: rgba(22, 82, 74, 0.12);
      --text: #153331;
      --muted: #5f7975;
      --normal: #2d8f69;
      --watch: #d18b21;
      --critical: #c23b3b;
      --shadow: 0 24px 60px rgba(21, 51, 49, 0.12);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", Tahoma, sans-serif;
      background:
        radial-gradient(circle at top left, rgba(46, 143, 105, 0.22), transparent 30%),
        linear-gradient(160deg, #edf5f2 0%, #dfeae7 100%);
      color: var(--text);
    }

    .page {
      max-width: 1180px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }

    .hero {
      display: grid;
      gap: 18px;
      margin-bottom: 28px;
    }

    .hero-card {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 24px;
      box-shadow: var(--shadow);
      padding: 24px;
      backdrop-filter: blur(12px);
    }

    .hero h1 {
      margin: 0 0 8px;
      font-size: clamp(2rem, 4vw, 3.4rem);
      line-height: 1.05;
    }

    .hero p, .subtle, .patient-meta, .team-line {
      color: var(--muted);
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 14px;
    }

    .metric, .patient-card {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 20px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }

    .metric {
      padding: 18px;
    }

    .metric strong {
      display: block;
      font-size: 1.7rem;
      margin-top: 8px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 18px;
      margin-top: 24px;
    }

    .patient-card {
      padding: 20px;
      position: relative;
      overflow: hidden;
    }

    .patient-card::after {
      content: "";
      position: absolute;
      inset: auto -40px -40px auto;
      width: 140px;
      height: 140px;
      background: radial-gradient(circle, rgba(255,255,255,0.65), rgba(255,255,255,0));
      pointer-events: none;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 0.88rem;
      text-transform: capitalize;
      color: white;
    }

    .pill.normal { background: var(--normal); }
    .pill.watch { background: var(--watch); }
    .pill.critical { background: var(--critical); }

    .vitals {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin: 16px 0;
    }

    .vital {
      background: rgba(21, 51, 49, 0.04);
      border-radius: 16px;
      padding: 12px;
    }

    .vital strong {
      display: block;
      font-size: 1.35rem;
      margin-top: 4px;
      color: var(--text);
    }

    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 18px;
    }

    button {
      border: 0;
      border-radius: 999px;
      padding: 10px 14px;
      font-weight: 600;
      cursor: pointer;
      color: white;
      background: #1f6f64;
    }

    button[data-mode="watch"] { background: #b67817; }
    button[data-mode="critical"] { background: #a93131; }
    button:hover { filter: brightness(1.05); }

    @media (max-width: 700px) {
      .page { padding: 20px 14px 34px; }
      .vitals { grid-template-columns: 1fr; }
      .actions { flex-direction: column; }
      button { width: 100%; }
    }
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="hero-card">
        <div class="subtle" id="hospital-name">Loading hospital dashboard...</div>
        <h1>Bedside Patient Monitoring</h1>
        <p>Prototype for a Raspberry Pi based hospital monitoring system that reads patient vitals, highlights risk, and routes updates to the assigned care team.</p>
      </div>
      <div class="metrics" id="metrics"></div>
    </section>

    <section class="grid" id="patient-grid"></section>
  </main>

  <script>
    async function fetchDashboard() {
      const response = await fetch('/api/dashboard');
      if (!response.ok) {
        throw new Error('Failed to load dashboard');
      }
      return response.json();
    }

    async function simulate(patientId, mode) {
      await fetch(`/api/patients/${patientId}/simulate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode })
      });
      await render();
    }

    function metricCard(label, value) {
      return `<article class="metric"><div class="subtle">${label}</div><strong>${value}</strong></article>`;
    }

    function patientCard(patient) {
      return `
        <article class="patient-card">
          <div class="pill ${patient.alert_level}">${patient.alert_level}</div>
          <h2>${patient.patient_name}</h2>
          <div class="patient-meta">${patient.bed_label} · ${patient.ward}</div>
          <p>${patient.status_note}</p>
          <div class="team-line">Assigned doctor: ${patient.assigned_doctor.name}</div>
          <div class="vitals">
            <div class="vital"><div class="subtle">Heart Rate</div><strong>${patient.vitals.heart_rate} bpm</strong></div>
            <div class="vital"><div class="subtle">SpO2</div><strong>${patient.vitals.spo2}%</strong></div>
            <div class="vital"><div class="subtle">Blood Pressure</div><strong>${patient.vitals.systolic_bp}/${patient.vitals.diastolic_bp}</strong></div>
            <div class="vital"><div class="subtle">Temperature</div><strong>${patient.vitals.temperature_c} C</strong></div>
          </div>
          <div class="subtle">Updated: ${new Date(patient.last_updated).toLocaleString()}</div>
          <div class="actions">
            <button data-mode="stable" onclick="simulate('${patient.id}', 'stable')">Set Stable</button>
            <button data-mode="watch" onclick="simulate('${patient.id}', 'watch')">Set Watch</button>
            <button data-mode="critical" onclick="simulate('${patient.id}', 'critical')">Set Critical</button>
          </div>
        </article>
      `;
    }

    async function render() {
      const data = await fetchDashboard();
      document.getElementById('hospital-name').textContent = `${data.hospital_name} · Generated ${new Date(data.generated_at).toLocaleString()}`;

      const critical = data.patients.filter(p => p.alert_level === 'critical').length;
      const watch = data.patients.filter(p => p.alert_level === 'watch').length;
      document.getElementById('metrics').innerHTML = [
        metricCard('Patients monitored', data.patients.length),
        metricCard('Critical alerts', critical),
        metricCard('Watch alerts', watch),
        metricCard('Stable patients', data.patients.length - critical - watch),
      ].join('');

      document.getElementById('patient-grid').innerHTML = data.patients.map(patientCard).join('');
    }

    render().catch(error => {
      document.getElementById('patient-grid').innerHTML = `<article class="patient-card"><h2>Dashboard error</h2><p>${error.message}</p></article>`;
    });
  </script>
</body>
</html>
"""
