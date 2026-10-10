from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


AlertLevel = Literal["normal", "watch", "critical"]


class CareTeamMember(BaseModel):
    role: str
    name: str
    contact: str


class VitalSigns(BaseModel):
    heart_rate: int = Field(ge=0, le=250)
    spo2: int = Field(ge=0, le=100)
    systolic_bp: int = Field(ge=0, le=300)
    diastolic_bp: int = Field(ge=0, le=200)
    temperature_c: float = Field(ge=25, le=45)
    respiratory_rate: int = Field(ge=0, le=80)


class PatientRecord(BaseModel):
    id: str
    bed_label: str
    patient_name: str
    age: int
    ward: str
    diagnosis: str
    alert_level: AlertLevel
    status_note: str
    last_updated: datetime
    assigned_doctor: CareTeamMember
    assigned_staff: list[CareTeamMember]
    vitals: VitalSigns


class PatientSummary(BaseModel):
    id: str
    bed_label: str
    patient_name: str
    ward: str
    alert_level: AlertLevel
    status_note: str
    last_updated: datetime
    assigned_doctor: CareTeamMember
    vitals: VitalSigns


class SimulationRequest(BaseModel):
    mode: Literal["stable", "watch", "critical"]


class DashboardPayload(BaseModel):
    hospital_name: str
    generated_at: datetime
    patients: list[PatientSummary]
