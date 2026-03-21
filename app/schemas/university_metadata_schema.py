from datetime import date, datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field


class UniversityMetadataBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: dict[str, Any] = Field(default_factory=dict)


class UniversityWrite(UniversityMetadataBase):
    university_key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    name_local: str = Field(default="", max_length=300)
    country: str = Field(min_length=1, max_length=120)
    city: str = Field(default="", max_length=120)
    location: str = Field(default="", max_length=250)
    website: str = Field(default="", max_length=500)
    established_year: int | None = Field(default=None, ge=1000, le=3000)
    university_type: str = Field(default="", max_length=120)
    campus_type: str = Field(default="", max_length=120)
    application_portal: str = Field(default="", max_length=500)
    default_language: str = Field(default="", max_length=60)
    description: str = Field(default="", max_length=20000)


class DepartmentWrite(UniversityMetadataBase):
    department_key: str = Field(min_length=1, max_length=200)
    university_key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    website: str = Field(default="", max_length=500)


class ProgramWrite(UniversityMetadataBase):
    program_key: str = Field(min_length=1, max_length=200)
    university_key: str = Field(min_length=1, max_length=200)
    department_key: str | None = Field(default=None, max_length=200)
    program_name: str = Field(min_length=1, max_length=300)
    name_local: str = Field(default="", max_length=300)
    degree_level: str = Field(min_length=1, max_length=32)
    duration_months: int | None = Field(default=None, ge=1, le=240)
    ects_credits: int | None = Field(default=None, ge=1, le=1000)
    tuition_fee: float | None = Field(default=None, ge=0)
    tuition_currency: str = Field(default="", max_length=12)
    language_primary: str = Field(default="", max_length=60)
    program_url: str = Field(default="", max_length=500)
    admission_type: str = Field(default="", max_length=40)
    study_mode: str = Field(default="", max_length=40)


class ProgramIntakeWrite(UniversityMetadataBase):
    program_key: str = Field(min_length=1, max_length=200)
    intake_term: str = Field(min_length=1, max_length=60)
    intake_year: int | None = Field(default=None, ge=1900, le=3000)
    application_open_date: date | None = None
    application_deadline: date | None = None
    priority_deadline: date | None = None
    document_deadline: date | None = None
    program_start: date | None = None
    is_rolling: bool = False


class ApplicationRouteWrite(UniversityMetadataBase):
    program_key: str = Field(min_length=1, max_length=200)
    applicant_type: str = Field(min_length=1, max_length=60)
    portal_url: str = Field(default="", max_length=500)
    application_fee: float | None = Field(default=None, ge=0)
    fee_currency: str = Field(default="", max_length=12)
    admission_type: str = Field(default="", max_length=40)


class ProgramRequirementWrite(UniversityMetadataBase):
    program_key: str = Field(min_length=1, max_length=200)
    applicant_type: str = Field(default="", max_length=60)
    requirement_type: str = Field(min_length=1, max_length=80)
    requirement_value: str = Field(min_length=1, max_length=4000)
    is_mandatory: bool = True


class LanguageRequirementWrite(UniversityMetadataBase):
    program_key: str = Field(min_length=1, max_length=200)
    applicant_type: str = Field(default="", max_length=60)
    language: str = Field(min_length=1, max_length=60)
    test_type: str = Field(min_length=1, max_length=80)
    min_score: str = Field(min_length=1, max_length=120)
    score_scale: str = Field(default="", max_length=80)


class ProfessorWrite(UniversityMetadataBase):
    professor_key: str = Field(min_length=1, max_length=200)
    university_key: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    title: str = Field(default="", max_length=120)
    department: str = Field(default="", max_length=300)
    research_interests: str = Field(default="", max_length=4000)
    email: str = Field(default="", max_length=320)
    website: str = Field(default="", max_length=500)


class LabWrite(UniversityMetadataBase):
    lab_key: str = Field(min_length=1, max_length=200)
    university_key: str = Field(min_length=1, max_length=200)
    lab_name: str = Field(min_length=1, max_length=300)
    research_focus: str = Field(default="", max_length=4000)
    lab_website: str = Field(default="", max_length=500)


class CourseWrite(UniversityMetadataBase):
    course_key: str = Field(min_length=1, max_length=200)
    university_key: str = Field(min_length=1, max_length=200)
    department_key: str | None = Field(default=None, max_length=200)
    course_name: str = Field(min_length=1, max_length=300)
    course_code: str = Field(default="", max_length=80)
    ects_credits: int | None = Field(default=None, ge=1, le=1000)
    language: str = Field(default="", max_length=60)
    level: str = Field(default="", max_length=40)
    course_url: str = Field(default="", max_length=500)


class ProgramCourseLinkWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    program_key: str = Field(min_length=1, max_length=200)
    course_key: str = Field(min_length=1, max_length=200)
    course_type: str = Field(default="optional", max_length=40)


class ProgramLabLinkWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    program_key: str = Field(min_length=1, max_length=200)
    lab_key: str = Field(min_length=1, max_length=200)


class ProgramProfessorLinkWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    program_key: str = Field(min_length=1, max_length=200)
    professor_key: str = Field(min_length=1, max_length=200)


class ProfessorLabLinkWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    professor_key: str = Field(min_length=1, max_length=200)
    lab_key: str = Field(min_length=1, max_length=200)


class SourceRecordWrite(UniversityMetadataBase):
    entity_type: str = Field(min_length=1, max_length=80)
    entity_key: str = Field(min_length=1, max_length=200)
    source_url: str = Field(min_length=1, max_length=1000)
    source_title: str = Field(default="", max_length=500)
    retrieved_at: datetime | None = None
    content_hash: str = Field(default="", max_length=128)
    extractor_version: str = Field(default="", max_length=80)
    confidence: float | None = Field(default=None, ge=0, le=1)
    raw_snippet: str = Field(default="", max_length=12000)


class UniversityMetadataIngestionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    universities: list[UniversityWrite] = Field(default_factory=list)
    departments: list[DepartmentWrite] = Field(default_factory=list)
    programs: list[ProgramWrite] = Field(default_factory=list)
    program_intakes: list[ProgramIntakeWrite] = Field(default_factory=list)
    application_routes: list[ApplicationRouteWrite] = Field(default_factory=list)
    program_requirements: list[ProgramRequirementWrite] = Field(default_factory=list)
    language_requirements: list[LanguageRequirementWrite] = Field(default_factory=list)
    professors: list[ProfessorWrite] = Field(default_factory=list)
    labs: list[LabWrite] = Field(default_factory=list)
    courses: list[CourseWrite] = Field(default_factory=list)
    program_courses: list[ProgramCourseLinkWrite] = Field(default_factory=list)
    program_labs: list[ProgramLabLinkWrite] = Field(default_factory=list)
    program_professors: list[ProgramProfessorLinkWrite] = Field(default_factory=list)
    professor_labs: list[ProfessorLabLinkWrite] = Field(default_factory=list)
    source_records: list[SourceRecordWrite] = Field(default_factory=list)
