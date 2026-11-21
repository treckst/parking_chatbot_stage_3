from datetime import datetime
from typing import Annotated, Any, Literal

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages
from pydantic import BaseModel, Field


class ReservationPeriod(BaseModel):
    start_date: str | None = Field(
        description="The start date with time from which the reservation is starting",
        default=None,
    )
    end_date: str | None = Field(
        description="The end date with time to which the reservation is due",
        default=None,
    )


class UserState(BaseModel):
    place: str | None = Field(default=None, description="Requested parking venue or address")
    name: str | None = Field(description="user name", default=None)
    car_number: str | None = Field(description="user car plate", default=None)
    reservation_period: ReservationPeriod = Field(
        description="Reservation period",
        default_factory=ReservationPeriod,
    )
    approval_time: datetime | None = Field(default=None)


# without approval_time for updating
class UserInfoExtraction(BaseModel):
    place: str | None = Field(default=None, description="Requested parking venue or address")
    name: str | None = None
    car_number: str | None = None
    reservation_period: ReservationPeriod = Field(default_factory=ReservationPeriod)


class AdminDecision(BaseModel):
    status: Literal["approved", "refused"] = Field(
        description="Final administrator decision: 'approved' or 'refused'."
    )
    reason: str | None = Field(
        default=None,
        description="Optional administrator comment or refusal reason.",
    )
    approval_time: datetime | None = Field(
        default=None,
        description="UTC timestamp when the administrator approved the reservation.",
    )


class SQLAvailabilityResult(BaseModel):
    summary: str = Field(description="Concise summary of the SQL availability lookup.")
    slot_available: bool = Field(
        description="True only if SQL results confirm at least one available slot matching the requested venue, date, and hours."
    )
    assigned_slot: str | None = Field(
        default=None,
        description="Selected available slot_code (e.g. 'A01') if slot_available is True, otherwise null.",
    )


class MainState(BaseModel):
    messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)
    summary: str = ""
    current_message: str = ""
    relevant_documents: list[str] = Field(default_factory=list)
    sql_result: str = ""
    slot_available: bool = False
    assigned_slot: str | None = None
    admin_decision: AdminDecision | None = None
    reservation_recorded: bool = False
    reservation_record_error: str | None = None
    user_info: UserState = Field(
        description="user's info for the reservation",
        default_factory=UserState,
    )
    missing_info: list[str] = Field(default_factory=list)
    wants_static_info: bool = False
    wants_dynamic_info: bool = False
    wants_reservation: bool = False
    dynamic_question: str = ""


class RetrievalInput(BaseModel):
    current_message: str = ""


class RetrievalOutput(BaseModel):
    relevant_documents: list[str] = Field(default_factory=list)


class RetrievalState(RetrievalInput, RetrievalOutput):
    run_query: str = ""
    max_retrieves: int = 0


class SQLAgentInput(BaseModel):
    wants_reservation: bool = False
    wants_dynamic_info: bool = False
    dynamic_question: str = ""
    missing_info: list[str] = Field(default_factory=list)
    user_info: UserState = Field(default_factory=UserState)


class SQLAgentOutput(BaseModel):
    sql_result: str = ""
    slot_available: bool = False
    assigned_slot: str | None = None


class SQLAgentState(SQLAgentInput, SQLAgentOutput):
    sql_calls: int = 0
    sql_messages: Annotated[list[BaseMessage], add_messages] = Field(default_factory=list)


class AdminAgentInput(BaseModel):
    user_info: UserState = Field(default_factory=UserState)
    assigned_slot: str | None = None
    sql_result: str = ""


class AdminAgentOutput(BaseModel):
    admin_decision: AdminDecision | None = None
    user_info: UserState = Field(default_factory=UserState)


class AdminAgentState(AdminAgentInput, AdminAgentOutput):
    request_id: str | None = None
    admin_request_message: str = ""
    raw_admin_response: Any = None


class UpgradedQuerySchema(BaseModel):
    finetuned_query: str = Field(description="upgraded query for search")


class FraudDetectSchema(BaseModel):
    safe: Literal[True, False] = Field(
        description="True if the query is safe, False if it's fraudulent."
    )
    reason: str | None = Field(description="explanation if unsafe", default=None)


class RefinedQuerySchema(BaseModel):
    query_relevant: Literal["relevant", "irrelevant"] = Field(
        "relevant if query has anything to do with the parking reservation purpose, "
        "irrelevant if query is not leading to the parking reservation"
    )


class GradeDocs(BaseModel):
    binary_score: Literal["yes", "no"] = Field(
        description="Documents are relevant to the question, 'yes' or 'no'"
    )


class DecideRetrieval(BaseModel):
    decision: Literal[True, False] = Field(
        description="True if the user message requires retrieval of information."
    )


class IntentSchema(BaseModel):
    static_info: bool = Field(
        description="True for static_info If user asks about general information, parking details, location, booking process"
    )
    dynamic_info: bool = Field(
        description="True if user asks about space availability, working hours, prices (the data that is frequently changes"
    )
    reservation: bool = Field(
        description="True if user does a reservation or provides a new userinfo for the reservation"
    )
    dynamic_question: str
