from __future__ import annotations

from datetime import datetime, timedelta
from threading import Lock

from medical_monitor_app.schemas import CareTeamMember, DashboardPayload, PatientRecord, PatientSummary, VitalSigns


class PatientStore:
    def __init__(self) -> None:
        self._lock = Lock()
        now = datetime.utcnow()
        self._patients: dict[str, PatientRecord] = {
            "bed-icu-01": PatientRecord(
                id="bed-icu-01",
                bed_label="ICU-01",
                patient_name="Rohan Mehta",
                age=58,
                ward="ICU",
                diagnosis="Post-operative observation",
                alert_level="normal",
                status_note="Stable and responding to treatment.",
                last_updated=now - timedelta(minutes=2),
                assigned_doctor=CareTeamMember(role="Doctor", name="Dr. A. Kapoor", contact="+91-90000-11111"),
                assigned_staff=[
                    CareTeamMember(role="Nurse", name="Anita Sharma", contact="+91-90000-22222"),
                    CareTeamMember(role="Technician", name="Vikram Das", contact="+91-90000-33333"),
                ],
                vitals=VitalSigns(
                    heart_rate=82,
                    spo2=98,
                    systolic_bp=122,
                    diastolic_bp=79,
                    temperature_c=36.8,
                    respiratory_rate=17,
                ),
            ),
            "bed-icu-02": PatientRecord(
                id="bed-icu-02",
                bed_label="ICU-02",
                patient_name="Fatima Khan",
                age=71,
                ward="ICU",
                diagnosis="Cardiac monitoring",
                alert_level="watch",
                status_note="Watch oxygen saturation and blood pressure trends.",
                last_updated=now - timedelta(minutes=1),
                assigned_doctor=CareTeamMember(role="Doctor", name="Dr. N. Iyer", contact="+91-90000-44444"),
                assigned_staff=[
                    CareTeamMember(role="Nurse", name="Rahul Sen", contact="+91-90000-55555"),
                ],
                vitals=VitalSigns(
                    heart_rate=96,
                    spo2=93,
                    systolic_bp=146,
                    diastolic_bp=91,
                    temperature_c=37.4,
                    respiratory_rate=22,
                ),
            ),
            "bed-ward-07": PatientRecord(
                id="bed-ward-07",
                bed_label="WARD-07",
                patient_name="Neha Verma",
                age=43,
                ward="Recovery",
                diagnosis="Respiratory support",
                alert_level="critical",
                status_note="Immediate staff review needed for low oxygen saturation.",
                last_updated=now,
                assigned_doctor=CareTeamMember(role="Doctor", name="Dr. S. Rao", contact="+91-90000-66666"),
                assigned_staff=[
                    CareTeamMember(role="Nurse", name="Pooja Nair", contact="+91-90000-77777"),
                    CareTeamMember(role="Respiratory", name="Arjun Patel", contact="+91-90000-88888"),
                ],
                vitals=VitalSigns(
                    heart_rate=118,
                    spo2=86,
                    systolic_bp=154,
                    diastolic_bp=98,
                    temperature_c=38.7,
                    respiratory_rate=28,
                ),
            ),
        }

    def list_summaries(self) -> list[PatientSummary]:
        with self._lock:
            patients = sorted(self._patients.values(), key=lambda item: item.bed_label)
            return [
                PatientSummary(
                    id=patient.id,
                    bed_label=patient.bed_label,
                    patient_name=patient.patient_name,
                    ward=patient.ward,
                    alert_level=patient.alert_level,
                    status_note=patient.status_note,
                    last_updated=patient.last_updated,
                    assigned_doctor=patient.assigned_doctor,
                    vitals=patient.vitals,
                )
                for patient in patients
            ]

    def get_patient(self, patient_id: str) -> PatientRecord | None:
        with self._lock:
            return self._patients.get(patient_id)

    def set_mode(self, patient_id: str, mode: str) -> PatientRecord | None:
        with self._lock:
            patient = self._patients.get(patient_id)
            if patient is None:
                return None

            if mode == "stable":
                patient.vitals = VitalSigns(
                    heart_rate=79,
                    spo2=98,
                    systolic_bp=120,
                    diastolic_bp=78,
                    temperature_c=36.9,
                    respiratory_rate=16,
                )
                patient.alert_level = "normal"
                patient.status_note = "Patient stable. Routine observation continues."
            elif mode == "watch":
                patient.vitals = VitalSigns(
                    heart_rate=101,
                    spo2=92,
                    systolic_bp=148,
                    diastolic_bp=94,
                    temperature_c=37.8,
                    respiratory_rate=23,
                )
                patient.alert_level = "watch"
                patient.status_note = "Vitals drifting. Assigned team should review soon."
            else:
                patient.vitals = VitalSigns(
                    heart_rate=126,
                    spo2=84,
                    systolic_bp=162,
                    diastolic_bp=102,
                    temperature_c=39.1,
                    respiratory_rate=31,
                )
                patient.alert_level = "critical"
                patient.status_note = "Critical alert. Immediate bedside assessment required."

            patient.last_updated = datetime.utcnow()
            return patient

    def dashboard_payload(self) -> DashboardPayload:
        return DashboardPayload(
            hospital_name="GlobalCare Monitoring Demo",
            generated_at=datetime.utcnow(),
            patients=self.list_summaries(),
        )


store = PatientStore()
