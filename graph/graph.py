from datetime import date, datetime, time as dt_time
import json

from dotenv import load_dotenv
from langchain.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState

load_dotenv()

from src.admin_graph import admin_agent
from src.functions import get_latest_human_message
from src.guardrails import InputSanitizer
from src.models_vector import llm, response_llm
from src.prompts import (
    EXTRACT_STATE_PROMPT,
    FINAL_RESPONSE_PROMPT,
    INTENTION_PROMPT,
    SECUTIRY_ALERT_PROMPT,
)
from src.react_graph import sql_agent
from src.reservation_recorder import save_approved_reservation
from src.retrieval_graph import retrieval_graph
from src.schema import (
    IntentSchema,
    MainState,
    UserInfoExtraction,
    UserState,
)
from src.security import SecutiryGuard

guard = SecutiryGuard()
sanitizer = InputSanitizer()

intent_llm = llm.with_structured_output(IntentSchema)
extractor_llm = llm.with_structured_output(UserInfoExtraction)
final_response_chain = (
    ChatPromptTemplate.from_template(FINAL_RESPONSE_PROMPT)
    | response_llm
    | StrOutputParser()
)


def validate_reservation(user_info: UserState) -> list[str]:
    missing: list[str] = []
    for label, value in (
        ("your name", user_info.name),
        ("your car registration number", user_info.car_number),
        ("the parking place", user_info.place),
    ):
        if not (value or "").strip():
            missing.append(label)

    period: list[datetime] = []
    known_date: date | None = None
    needs_date = False
    has_period_error = False

    date_fields = (
        ("arrival", user_info.reservation_period.start_date),
        ("departure", user_info.reservation_period.end_date),
    )
    for role, value in date_fields:
        raw = (value or "").strip()
        if not raw:
            missing.append(f"the {role} time" if known_date else f"the {role} date and time")
            continue

        if len(raw) <= 10:
            try:
                parsed_date = date.fromisoformat(raw)
            except ValueError:
                try:
                    dt_time.fromisoformat(raw)
                    needs_date = True
                    continue
                except ValueError:
                    missing.append(f"the {role} date and time")
                    continue

            if (parsed_date.year, parsed_date.month) != (2026, 11):
                missing.append("a valid date in November 2026 (reservations cover 1–30 November 2026)")
                has_period_error = True
                break
            known_date = parsed_date
            missing.append(f"the {role} time")
            continue

        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            missing.append(f"the {role} date and time")
            continue

        if moment.tzinfo is not None:
            missing.append(f"the {role} date and time in Warsaw local time (without a timezone offset)")
            has_period_error = True
            break
        if (moment.year, moment.month) != (2026, 11):
            missing.append("a valid date in November 2026 (reservations cover 1–30 November 2026)")
            has_period_error = True
            break
        known_date = moment.date()
        period.append(moment)

    if needs_date and not has_period_error:
        missing.append("the parking date")

    if not has_period_error and len(period) == 2:
        start, end = period
        if end.date() != start.date() or end <= start:
            missing.append("a departure time after arrival on the same day")

    return missing


def route_entry(state: MainState):
    cleared_msg = sanitizer.sanitize(state.messages[-1].content)
    if sanitizer.is_suspicious(cleared_msg)[0] or not guard.llm_check(cleared_msg)["safe"]:
        return "security_stoppage"
    if len(state.messages) > 15:
        return "summarize_conversation"
    return "check_intent"


def security_stoppage(state: MainState):
    human_msg = get_latest_human_message(state.messages)
    response = llm.invoke([SystemMessage(content=SECUTIRY_ALERT_PROMPT), human_msg])
    return {"messages": [response]}


def summarize_conversation(state: MainState):
    if state.summary:
        summary_message = (
            f"This is summary of the conversation to date: {state.summary}\n\n"
            "Extend the summary by taking into account the new messages above:"
        )
    else:
        summary_message = "Create a summary of the conversation above:"
    response = llm.invoke(state.messages + [HumanMessage(content=summary_message)])
    delete_messages = [RemoveMessage(id=m.id) for m in state.messages[:-4]]
    return {"summary": response.content, "messages": delete_messages}


def check_intent(state: MainState):
    latest_human = get_latest_human_message(state.messages)
    current_message = sanitizer.sanitize(latest_human.content)
    recent_messages = [
        msg for msg in state.messages if isinstance(msg, (HumanMessage, AIMessage))
    ][-3:]

    intent = intent_llm.invoke([
        SystemMessage(content=INTENTION_PROMPT.format(
            missing_info=", ".join(state.missing_info) or "None"
        )),
        *recent_messages,
    ])
    return {
        "current_message": current_message,
        "wants_static_info": intent.static_info,
        "wants_dynamic_info": intent.dynamic_info,
        "wants_reservation": intent.reservation,
        "dynamic_question": intent.dynamic_question,
        "relevant_documents": [],
        "sql_result": "",
        "slot_available": False,
        "assigned_slot": None,
        "admin_decision": None,
        "reservation_recorded": False,
        "reservation_record_error": None,
    }


def route_after_intent(state: MainState):
    if state.wants_reservation:
        return "update_state"
    if state.wants_static_info:
        return "retrieval"
    if state.wants_dynamic_info:
        return "sql_agent"
    return "generate_response"


def update_state(state: MainState):
    extracted_info = extractor_llm.invoke([
        SystemMessage(content=EXTRACT_STATE_PROMPT),
        HumanMessage(content=json.dumps({
            "current_message": state.current_message,
            "existing_user_info": state.user_info.model_dump(mode="json"),
            "missing_fields": state.missing_info,
        })),
    ])
    existing_info = state.user_info.model_dump()
    current_info = extracted_info.model_dump(exclude_none=True)
    current_reservation_period = current_info.pop("reservation_period", {})

    merged = {
        **existing_info,
        **current_info,
        "reservation_period": {
            **existing_info["reservation_period"],
            **current_reservation_period,
        },
    }
    user_info = UserState.model_validate(merged)
    missing = validate_reservation(user_info)
    return {
        "user_info": user_info,
        "missing_info": missing,
    }


def route_after_update(state: MainState):
    if state.wants_static_info:
        return "retrieval"
    if state.wants_dynamic_info or not state.missing_info:
        return "sql_agent"
    return "generate_response"


def route_after_retrieval(state: MainState):
    if state.wants_dynamic_info or (state.wants_reservation and not state.missing_info):
        return "sql_agent"
    return "generate_response"


def route_after_sql(state: MainState):
    if state.wants_reservation and not state.missing_info and state.slot_available:
        return "admin_agent"
    return "generate_response"


def route_after_admin(state: MainState):
    if state.admin_decision and state.admin_decision.status == "approved":
        return "record_reservation"
    return "generate_response"


def record_reservation(state: MainState):
    if not (state.admin_decision and state.admin_decision.status == "approved"):
        return {
            "reservation_recorded": False,
            "reservation_record_error": "Reservation was not approved by an administrator.",
        }

    start = state.user_info.reservation_period.start_date or ""
    end = state.user_info.reservation_period.end_date or ""
    period = f"{start} - {end}"
    location_parts = [p for p in (state.user_info.place, f"slot {state.assigned_slot}" if state.assigned_slot else None) if p]
    if location_parts:
        period = f"{period} ({', '.join(location_parts)})"

    approval_dt = state.admin_decision.approval_time or state.user_info.approval_time
    result = save_approved_reservation.invoke({
        "name": state.user_info.name or "",
        "car_number": state.user_info.car_number or "",
        "reservation_period": period,
        "approval_time": approval_dt.isoformat() if approval_dt else None,
    })
    return {
        "reservation_recorded": bool(result.get("ok")),
        "reservation_record_error": result.get("error"),
    }


def generate_response(state: MainState):
    if not (state.wants_static_info or state.wants_dynamic_info or state.wants_reservation):
        return {
            "messages": [
                AIMessage(content="I can answer parking questions or help you start a reservation.")
            ]
        }

    if state.wants_static_info:
        static_context = (
            "\n\n".join(state.relevant_documents)
            if state.relevant_documents
            else "No relevant static parking documents found."
        )
    else:
        static_context = "None"

    answer = final_response_chain.invoke({
        "summary": state.summary or "None",
        "query": state.current_message,
        "static_context": static_context,
        "sql_context": state.sql_result or "None",
        "user_info": state.user_info.model_dump(mode="json") if state.wants_reservation else "None",
        "missing_info": (
            ", ".join(state.missing_info) if state.wants_reservation and state.missing_info else "None"
        ),
        "admin_decision": (
            state.admin_decision.model_dump(mode="json") if state.admin_decision else "None"
        ),
        "reservation_recorded": state.reservation_recorded,
        "reservation_record_error": state.reservation_record_error or "None",
    })
    return {"messages": [AIMessage(content=answer)]}


workflow = StateGraph(MainState, input_schema=MessagesState, output_schema=MessagesState)

workflow.add_node("security_stoppage", security_stoppage)
workflow.add_node("summarize_conversation", summarize_conversation)
workflow.add_node("check_intent", check_intent)
workflow.add_node("update_state", update_state)
workflow.add_node("retrieval", retrieval_graph)
workflow.add_node("sql_agent", sql_agent)
workflow.add_node("admin_agent", admin_agent)
workflow.add_node("record_reservation", record_reservation)
workflow.add_node("generate_response", generate_response)

workflow.add_conditional_edges(START, route_entry, [
    "security_stoppage", "summarize_conversation", "check_intent",
])
workflow.add_edge("security_stoppage", END)
workflow.add_edge("summarize_conversation", "check_intent")
workflow.add_conditional_edges("check_intent", route_after_intent, [
    "update_state", "retrieval", "sql_agent", "generate_response",
])
workflow.add_conditional_edges("update_state", route_after_update, [
    "retrieval", "sql_agent", "generate_response",
])
workflow.add_conditional_edges("retrieval", route_after_retrieval, [
    "sql_agent", "generate_response",
])
workflow.add_conditional_edges("sql_agent", route_after_sql, [
    "admin_agent", "generate_response",
])
workflow.add_conditional_edges("admin_agent", route_after_admin, [
    "record_reservation", "generate_response",
])
workflow.add_edge("record_reservation", "generate_response")
workflow.add_edge("generate_response", END)

graph = workflow.compile()
