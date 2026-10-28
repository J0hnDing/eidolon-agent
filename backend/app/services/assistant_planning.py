"""Editable planning defaults and verifiable outcomes for ordinary agenda tasks."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update

from app.models import ActLiveMessage, ActSession, ActTurn, AssistantAgendaItem, AssistantState

LEGACY_MORNING_NOTES = (
    "Review current circumstances and plan the day. Call assistant.changes.list and follow its "
    "pagination to review backend-detected changes; retrieve authoritative details as needed. "
    "Review the agenda, advance authorized work, and schedule useful questions considerately. "
    "Leave unresolved responsibilities in the agenda. Communicate only useful results; an empty "
    "final response is appropriate when nothing needs the user's attention."
)

PREVIOUS_MORNING_NOTES = """Plan today and the coming days around your two jobs: understand the user and help the user. Call assistant.changes.list and follow its pagination, inspect the agenda and whole question list, and read relevant current Atlas details, commitments, todos and recent summaries.

Consider the person as well as their obligations: what deserves attention, practical help, conversation or personal follow-through? Save worthwhile intentions as agenda tasks and questions with suitable times or clear dependencies. Build on existing responsibilities and answers, and revise stale plans. Their agenda activations carry out the work and conversations.

Check current.daily_interaction and plan an opportunity for today's required interaction if needed. Review the saved agenda for useful next steps and timing, then finish silently."""

PREVIOUS_STATES_MORNING_NOTES = """Plan today and the coming days around your two jobs: understand the user and help the user. Call assistant.changes.list and follow its pagination, inspect the agenda and whole question list, and read relevant current Atlas details, commitments, todos and recent summaries. Read current Atlas States through atlas.states.get without an at timestamp, including individual goal, interest and relationship estimates, recency and confidence; read relevant configuration and history when needed to understand them.

Consider States alongside the user's wishes and circumstances when choosing useful help, conversation and follow-through; they do not dictate the plan. Treat stale progress or uncertain satisfaction as a reason to understand the user's current priorities, not proof of disinterest. Where useful, ask whether a goal or interest still matters or offer to find a free block for it. Ignore physical_activity and mental_stimulation for now; physical_energy and mental_energy remain relevant.

Save worthwhile intentions as agenda tasks and questions with suitable times or clear dependencies. Build on existing responsibilities and answers, and revise stale plans. Their agenda activations carry out the work and conversations.

Check current.daily_interaction and plan an opportunity for today's required interaction if needed. Review the saved agenda for useful next steps and timing, then finish silently."""

PREVIOUS_TARGETS_MORNING_NOTES = PREVIOUS_STATES_MORNING_NOTES.replace(
    "read relevant configuration and history when needed to understand them.",
    "read relevant configuration and history when needed to understand them. "
    "Goals and Interests jointly inform the existing goal_progression State; "
    "personal Relationships inform the existing close_connection State. "
    "Read their individual estimates and uncertainty rather than treating either summary as the whole picture.",
)

PREVIOUS_BALANCED_MORNING_NOTES = """Plan today and the coming days around both of your jobs: do concrete useful work for the user and maintain a personal relationship through casual conversation and company. Use today's date in current.timezone, including after downtime. Call assistant.changes.list and follow pagination, inspect the full agenda and whole question list including answers, and read relevant current Atlas details, commitments, todos and recent summaries. Read current Atlas States through atlas.states.get without an at timestamp, including individual goal, interest and relationship estimates, recency and confidence; read relevant configuration and history when needed. Goals and Interests jointly inform the existing goal_progression State; personal Relationships inform the existing close_connection State. States inform judgment rather than dictate the plan. Ignore physical_activity and mental_stimulation for now; physical_energy and mental_energy remain relevant. Stale or uncertain evidence is not proof of the user's current priorities or feelings.

Save at least five distinct actionable responsibilities for today: two concrete work items, two personal interactions, and one other useful item of your choosing. These are minimums, not a fixed script or a reason to manufacture busywork. Work means taking ownership of an actual useful result: bounded research, preparation, organization, solving a problem or advancing an authorized goal. Reviewing context, planning, creating reminders and asking the user to do the work are not concrete work. Interactions can be a personal follow-up, casual conversation, sharing something the user may enjoy, or a mental/physical wellbeing check-in; no prescribed questions, topics or formats. The other item may be any additional useful responsibility, including more work or conversation, but must be distinct from the four required items.

Reuse and deliberately schedule existing responsibilities when suitable instead of creating duplicates. Each pending selected item needs a real due_at today at a considerate time; preserve dependencies and actual blockers in notes. Do not select recurring routines, this briefing, already waiting unanswered questions, approval-blocked work or a blocked dependency as if it were ready work. Find useful work within existing authority instead. Do not reopen or repeat an unanswered question to fill a minimum. A user reply is not required for an interaction to count as initiated. Respect explicit no-contact requests and quiet hours; report an actual constraint rather than bypass it to satisfy a quota. Record intended outcomes, source context and next steps in the agenda. Let their later activations carry out the work and conversations.

After saving and checking the agenda, call assistant.plan.save with work, interaction and other arrays of agenda IDs. The backend requires at least 2/2/1 distinct, nonrecurring items with today's actual scheduling or execution evidence. Existing completed work or delivered interactions today may count with real evidence; each item belongs to only one category. Review current.daily_plan on later turns, carry out pending responsibilities, record results and delivery, and revise or replace blocked/stale items. Saving the plan is not completing the work.

Then send the morning feed as your final response through normal chat delivery. Include today's weather and today's agenda, distinguishing the user's commitments from what you will handle. Beyond those anchors, freely include information worth the user's attention: relevant discoveries, opportunities, concerns, approaching deadlines or useful connections. Choose a natural structure and length for today's content; there is no fixed template or mandatory news section. Be selective, avoid internal bookkeeping, and do not present planned work as completed. This feed alone does not fulfill either planned personal interaction.

Use notion.daily_feed.read as an optional source, checking the report's own local date rather than only its edit timestamp. Read fresh calendar/todo or other available authoritative sources when needed. If the report is stale or unavailable, use current sources available within your permissions and briefly disclose any weather or agenda information you could not verify. Never present old weather/events as current or let one missing source prevent the rest of the briefing. Do not run or enable the standalone feed producer. Keep substantial research in its own agenda activation so the morning message is not delayed indefinitely.

Leave this recurring task open. On a retry, inspect the previous attempt and saved plan/delivery evidence, reuse valid planned items, repair deficits, and avoid repeating an already delivered briefing from this occurrence. Otherwise finish with the actual morning feed, not silently."""

PREVIOUS_REVIEW_MORNING_NOTES = """Review the user's life and take responsibility for useful help today, alongside personal conversation and company. Use today's date in current.timezone, including after downtime.

Read current todos and goals every morning, even when nothing changed. Call notion.todo.list(page_size=100) and follow every next_cursor to the end; review all unfinished, unarchived items, including overdue, due-soon, unscheduled and blocked work. Call atlas.goal.list(limit=100) without pursuit_status or horizon filters and review current goals, success conditions, blueprints, progress, dependencies and priorities. If that list reaches its limit, use the full paginated Goal record search with native data instead; do not assume a limited list is complete. Reviewing only changes, a previous summary or the Daily Feed does not replace either fresh read. Call assistant.changes.list and follow pagination for other changes, then inspect the full agenda and whole question list including prior answers. Read relevant commitments, calendar, recent summaries and current Atlas States through atlas.states.get without an at timestamp. Goals and Interests jointly inform the existing goal_progression State; personal Relationships inform the existing close_connection State. Consider individual estimates, uncertainty and recency alongside the user's wishes. Ignore physical_activity and mental_stimulation for now; physical_energy and mental_energy remain relevant.

Reconcile the backlog with the goals before choosing work. An old unchecked item is not proof it remains unfinished, and a planning target is not necessarily an official deadline. Check available completion evidence, current requirements, dependencies and recent user decisions. Notice approaching consequences, stalled goals and contradictions even when the records have not changed. Do not silently postpone, abandon or mark something done to tidy the list. Do not restart paused or abandoned goals without the user's direction.

For each responsibility needing attention, choose a concrete next step. Take ownership of work you can do within existing authority: research, prepare, organize, draft or solve the problem rather than tell the user to do it. Schedule ready substantial work at the earliest useful agenda activation, due now when practical; do not defer it to another day merely because this is planning. Small useful preparatory steps can be handled now. Save intended outcomes, source links, dependencies and next steps, and record actual results when completed. Prioritize meaningful help from existing todos and goals before inventing unrelated work. Larger work and remaining steps stay scheduled rather than delaying the morning message indefinitely.

When progress needs the user's choice, missing information, participation or approval, bring them the specific question, decision or reviewable material. Prepare whatever you can first. Reuse the existing question and prior answer; ask a new or still-open blocking question through assistant.question.ask at a suitable time today, promptly when the answer blocks useful work. Group closely related decisions to avoid a barrage. Do not leave an unresolved need only in notes or an unscheduled question. An already asked unanswered question stays waiting; do not repeat it or treat silence as an answer. Keep the blocked responsibility linked to that request and resume from the reply. If the user must do the personal or assessed part, still handle permitted preparation and explain the next action. Follow existing permission, academic and approval boundaries.

Save at least five distinct actionable responsibilities for today: two concrete work items, two personal interactions and one other useful item of your choosing. Existing reviewed responsibilities and actual deliveries today can count. Interactions can include personal follow-ups, casual conversation, shared interests or wellbeing; leave room for company beyond todo administration. Avoid duplicate work, filler and repeated questions. Give pending selected items a real due_at today. Blocked responsibilities remain tracked and discussed but do not substitute for ready concrete work. Select useful alternatives within your authority. Respect explicit no-contact requests and quiet hours. Call assistant.plan.save with work, interaction and other ID arrays after checking the saved agenda. Review current.daily_plan on later turns and follow through; saving intentions is not completing them.

Then send a natural morning feed with today's weather and agenda, distinguishing the user's commitments from what you will handle. Include material overdue issues, next steps and needs for the user's input; do not bury them behind unrelated discoveries. Add any other information worth their attention with no fixed layout, topic list or mandatory news section. Be selective, truthful and conversational. Ask tracked questions through the question tool and do not repeat already sent wording in the feed. The briefing alone does not fulfill the two personal interactions.

The Notion Daily Feed is an optional source, not a substitute for reading todos and goals. Check the report's own date before using time-sensitive contents. Use current sources within your permissions for the calendar and weather. If any source is unavailable, make the permitted attempt, disclose the specific gap and any input/access needed, and continue with verified information; do not claim an empty backlog or invent current facts. Do not run or enable the standalone producer.

Keep these notes as the reusable procedure; store daily decisions and outcomes in the relevant agenda items, source records or daily summary rather than appending dated instructions here. Leave this recurrence open. On retries, read todos and goals afresh, inspect prior effects, reuse valid saved items and avoid resending an already delivered briefing from this occurrence. Otherwise finish with the actual feed, not silently."""

PREVIOUS_BRIEFING_MORNING_NOTES = """Plan today from a fresh review of the user's current responsibilities and goals. Use today's date in current.timezone, including after downtime.

Read current todos and goals every morning, even when nothing changed. Call notion.todo.list(page_size=100) and follow every next_cursor to the end; review all unfinished, unarchived items, including overdue, due-soon, unscheduled and blocked work. Call atlas.goal.list(limit=100) without pursuit_status or horizon filters and review current goals, success conditions, blueprints, progress, dependencies and priorities. If that list reaches its limit, use the full paginated Goal record search with native data instead; do not assume a limited list is complete. Changes, previous summaries and the Daily Feed do not replace either fresh read. Call assistant.changes.list and follow pagination for other changes, then inspect the full agenda and whole question list including prior answers. Read today's calendar, relevant commitments, recent summaries and current Atlas States through atlas.states.get without an at timestamp; interpret them using the shared Assistant instructions.

Reconcile the backlog with the goals before selecting today's responsibilities. Check available completion evidence, current requirements, deadlines, dependencies and recent user decisions. Distinguish personal planning targets from official deadlines. Identify material overdue issues, approaching consequences, stalled goals, contradictions and useful work ready to advance. For work needing the user's input, prepare the specific question or reviewable material and link it to the blocked responsibility using the shared agenda and question rules.

Save at least five distinct, nonrecurring responsibilities for today: two concrete work tasks, two personal interactions and one other useful item. Reuse reviewed existing responsibilities and actual work or interactions delivered today when suitable. The briefing and recurring routines do not fill these slots. Blocked responsibilities stay tracked but do not substitute for ready work; select useful alternatives. Give pending selections a real due_at today, due now when practical. Keep substantial execution in its own activation so the briefing can go out promptly. After checking the saved agenda, call assistant.plan.save with work, interaction and other ID arrays.

Send the morning feed as your final response through normal chat delivery. Include today's weather and agenda, distinguishing the user's commitments from what you will handle, plus material overdue issues, next steps and decisions needing their input. Tracked questions go through assistant.question.ask; the feed need not repeat a question already delivered. The briefing alone does not fulfill either planned personal interaction.

The Notion Daily Feed is an optional source. Check the report's own local date before using time-sensitive contents; use current sources for weather and calendar. If a source is unavailable, make the permitted attempt, identify the missing coverage and any needed input/access, and continue with the available information. Do not present an unavailable backlog as empty or stale weather/events as current. Do not run or enable the standalone feed producer.

Keep these notes as the reusable procedure; save daily decisions and outcomes in the relevant agenda items, source records or daily summary. Leave this recurrence open. On retries, read todos and goals afresh, inspect prior effects, reuse valid saved items and avoid resending an already delivered briefing from this occurrence. Otherwise finish with the actual feed."""

PREVIOUS_FLEXIBLE_MORNING_NOTES = """Plan today from a fresh review of the user's current responsibilities and goals. Use today's date in current.timezone, including after downtime.

Read current todos and goals every morning, even when nothing changed. Call notion.todo.list(page_size=100) and follow every next_cursor to the end; review all unfinished, unarchived items, including overdue, due-soon, unscheduled and blocked work. Call atlas.goal.list(limit=100) without pursuit_status or horizon filters and review current goals, success conditions, blueprints, progress, dependencies and priorities. If that list reaches its limit, use the full paginated Goal record search with native data instead; do not assume a limited list is complete. Changes, previous summaries and the Daily Feed do not replace either fresh read. Call assistant.changes.list and follow pagination for other changes, then inspect the full agenda and whole question list including prior answers. Read today's calendar, relevant commitments, recent summaries and current Atlas States through atlas.states.get without an at timestamp; interpret them using the shared Assistant instructions.

Reconcile the backlog with the goals before selecting today's responsibilities. Check available completion evidence, current requirements, deadlines, dependencies and recent user decisions. Distinguish personal planning targets from official deadlines. Identify material overdue issues, approaching consequences, stalled goals, contradictions and useful work ready to advance. For work needing the user's input, prepare the specific question or reviewable material and link it to the blocked responsibility using the shared agenda and question rules.

Save at least five distinct, nonrecurring responsibilities for today: two concrete work tasks, two personal interactions and one other useful item. Reuse reviewed existing responsibilities and actual work or interactions delivered today when suitable. The briefing and recurring routines do not fill these slots. Blocked responsibilities stay tracked but do not substitute for ready work; select useful alternatives. Give pending selections a real due_at today, due now when practical. Keep substantial execution in its own activation so the morning message can go out promptly. After checking the saved agenda, call assistant.plan.save with work, interaction and other ID arrays.

Then send a useful morning message based on the user's actual circumstances. Choose its content and shape for this person and this moment; there are no required topics, sections, ordering or length. Reading the calendar, backlog and saved plan is preparation, not a request to summarize them to the user. When their actual plans are unclear, a natural question can be more useful than reciting the recorded schedule. Use existing answers and fit the question to the time of day.

Examples of possible contributions, not a checklist or fixed wording:
- Check a relevant plan: "You've got quant research this morning. Are you still doing that? What else have you got planned today?"
- Share a discovery that changes a decision or saves effort, explaining the useful finding directly.
- Mention weather when plans suggest an outing or commute and it would help them prepare, such as rain before their walk home.
- Bring a timely choice, offer concrete help with something on their mind, or continue a conversation they care about.

A useful question can itself be the morning message. Deliver through the normal final response, or assistant.question.ask for a tracked question; do not repeat a question already delivered. The morning message alone does not fulfill either planned personal interaction.

The Notion Daily Feed is an optional source, used only when relevant. Check the report's own local date and verify time-sensitive information you choose to share. Weather need not be fetched when it has no bearing on the user's plans. Continue with available information when a source fails; mention an access gap only when it materially affects the help or decision at hand. Do not present an unavailable backlog as empty or old information as current. Do not run or enable the standalone feed producer.

Keep these notes as the reusable procedure; save daily decisions and outcomes in the relevant agenda items, source records or daily summary. Leave this recurrence open. On retries, read todos and goals afresh, inspect prior effects, reuse valid saved items and avoid repeating a morning message already delivered for this occurrence. Otherwise finish with the useful message."""

MORNING_NOTES = """Plan today from a fresh review of the user's current responsibilities and goals. Use today's date in current.timezone, including after downtime.

Read current todos and goals every morning, even when nothing changed. Call notion.todo.list(page_size=100) and follow every next_cursor to the end; review all unfinished, unarchived items, including overdue, due-soon, unscheduled and blocked work. Call atlas.goal.list(limit=100) without pursuit_status or horizon filters and review current goals, success conditions, blueprints, progress, dependencies and priorities. If that list reaches its limit, use the full paginated Goal record search with native data instead; do not assume a limited list is complete. Changes, previous summaries and the Daily Feed do not replace either fresh read. Call assistant.changes.list and follow pagination for other changes, then inspect the full agenda and whole question list including prior answers. Read today's calendar, relevant commitments, recent summaries and current Atlas States through atlas.states.get without an at timestamp; interpret them using the shared Assistant instructions.

Turn the overdue review into concrete help before choosing unrelated new work. Reconcile todos with current goals, requirements, completion evidence, dependencies and user decisions; prioritize by consequences, importance and how long work has been stuck. For each overdue item or coherent group, take or arrange a useful next step: verify completion, do an authorized part yourself, prepare material for approval, ask for missing information, or deliberately defer with a specific reason and next review time. Do small ready steps now and schedule substantial work promptly through the agenda, due now when practical. Link responsibilities to the original todos and record outcomes in their existing agenda notes. Research should remove a real obstacle; more research or a logistics sheet is not a substitute for doing ready work or asking the question that blocks it. Do not change dates, mark work complete or abandon goals merely to tidy the backlog.

An unchecked past-due todo has uncertain status, not an exemption from attention. When evidence does not establish whether it is done, ask a focused status question that enables help. Group related items where useful and pace questions by urgency and answering effort rather than sending a reminder for every row. Flexible deadlines, practice targets and no-late-penalty policies change priority; they do not establish completion or justify leaving work unattended. Clarify what remains and offer or take the useful next step.

Review unanswered questions alongside the work they block. Reuse known answers and respect explicit refusals, pauses and no-contact requests. An unanswered question may warrant a purposeful follow-up when a deadline approaches, circumstances change, work remains blocked, or enough time has passed for its urgency. Earlier notes saying to keep waiting describe a previous decision to reconsider; honor any underlying explicit user refusal, pause or no-contact request. Refine or deliberately reopen the existing question with the current reason, ask it through assistant.question.ask when useful now, or schedule it for a suitable time today. Explain what help the answer unlocks when useful. Avoid duplicate questions and repeated messages in the same exchange; do not treat avoiding repetition as a permanent ban on reasoned follow-up. Keep the blocked responsibility linked to the question and resume the work when answered. Silence is never consent.

Save at least five distinct, nonrecurring responsibilities for today: two concrete work tasks, two personal interactions and one other useful item. Choose work from the concrete next steps above before inventing optional new research. Meeting these minimums does not replace useful action on the overdue backlog. Reuse reviewed existing responsibilities and actual work or interactions delivered today when suitable. The briefing and recurring routines do not fill these slots. Blocked responsibilities stay tracked but do not substitute for ready work; select useful alternatives while addressing their actual blockers. Give pending selections a real due_at today, due now when practical. Keep substantial execution in its own activation so the morning message can go out promptly. After checking the saved agenda, call assistant.plan.save with work, interaction and other ID arrays.

Then send a useful morning message based on the user's actual circumstances. Choose its content and shape for this person and this moment; there are no required topics, sections, ordering or length. Reading the calendar, backlog and saved plan is preparation, not a request to summarize them to the user. When their actual plans are unclear, a natural question can be more useful than reciting the recorded schedule. Use existing answers and fit the question to the time of day.

Examples of possible contributions, not a checklist or fixed wording:
- Check a relevant plan: "You've got quant research this morning. Are you still doing that? What else have you got planned today?"
- Unblock overdue work: "Your referee requests were planned for yesterday. Have you contacted anyone yet? If not, I can help prepare the messages."
- Share a discovery that changes a decision or saves effort, explaining the useful finding directly.
- Mention weather when plans suggest an outing or commute and it would help them prepare, such as rain before their walk home.
- Bring a timely choice, offer concrete help with something on their mind, or continue a conversation they care about.

A useful question can itself be the morning message. Deliver through the normal final response, or assistant.question.ask for a tracked question; do not repeat a live question in the final reply. The morning message alone does not fulfill either planned personal interaction.

The Notion Daily Feed is an optional source, used only when relevant. Check the report's own local date and verify time-sensitive information you choose to share. Weather need not be fetched when it has no bearing on the user's plans. Continue with available information when a source fails; mention an access gap only when it materially affects the help or decision at hand. Do not present an unavailable backlog as empty or old information as current. Do not run or enable the standalone feed producer.

Keep these notes as the reusable procedure; save daily decisions and outcomes in the relevant agenda items, source records or daily summary. Leave this recurrence open. On retries, read todos and goals afresh, inspect prior effects, reuse valid saved items and avoid repeating a morning message already delivered for this occurrence. Otherwise finish with the useful message."""

DAILY_INTERACTION_NOTES = """Ensure today's meaningful Assistant interaction. Check current.daily_interaction; if already satisfied, finish silently. Otherwise use your understanding and the current agenda to choose a personal check-in, a worthwhile question, company or a useful result. Ask agenda questions through assistant.question.ask, or send one brief natural final message. Fit the timing to the user's commitments and quiet hours. A user reply is not required; finish after there is actual user-visible contact."""


def _upgrade_morning_notes(db) -> None:
    """Exact-content upgrades also reach installations with the old migration receipt."""
    untouched = (
        AssistantAgendaItem.notes.in_((LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES,
                                      PREVIOUS_STATES_MORNING_NOTES, PREVIOUS_TARGETS_MORNING_NOTES,
                                      PREVIOUS_BALANCED_MORNING_NOTES, PREVIOUS_REVIEW_MORNING_NOTES,
                                      PREVIOUS_BRIEFING_MORNING_NOTES, PREVIOUS_FLEXIBLE_MORNING_NOTES)),
        AssistantAgendaItem.recurrence_json["frequency"].as_string().is_not(None),
        AssistantAgendaItem.kind == "task",
    )
    if db.scalar(select(AssistantAgendaItem.id).where(*untouched).limit(1)) is None:
        return
    db.execute(update(AssistantAgendaItem).where(*untouched).values(
        notes=MORNING_NOTES, completion_requirement="morning_plan",
        revision=AssistantAgendaItem.revision + 1, updated_at=datetime.now(UTC)))
    db.commit()


def migrate_planning_contract(db, state: AssistantState) -> None:
    """Upgrade untouched defaults once; preserve customized routines and deletions."""
    if state.planning_contract_migrated:
        _upgrade_morning_notes(db)
        return
    db.execute(update(AssistantState).where(AssistantState.id == state.id).values(id=state.id))
    db.refresh(state)
    if state.planning_contract_migrated:
        _upgrade_morning_notes(db)
        return
    from app.services.assistant_recurrence import next_at, next_item_id

    now = datetime.now(UTC)
    for item in db.scalars(select(AssistantAgendaItem).where(
        or_(AssistantAgendaItem.notes.in_((LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES,
                PREVIOUS_STATES_MORNING_NOTES, PREVIOUS_TARGETS_MORNING_NOTES,
                PREVIOUS_BALANCED_MORNING_NOTES, PREVIOUS_REVIEW_MORNING_NOTES,
                PREVIOUS_BRIEFING_MORNING_NOTES, PREVIOUS_FLEXIBLE_MORNING_NOTES, MORNING_NOTES)),
            AssistantAgendaItem.title == "Morning planning"),
        AssistantAgendaItem.recurrence_json["frequency"].as_string().is_not(None),
        AssistantAgendaItem.kind == "task",
    )):
        if item.notes in (LEGACY_MORNING_NOTES, PREVIOUS_MORNING_NOTES, PREVIOUS_STATES_MORNING_NOTES,
                          PREVIOUS_TARGETS_MORNING_NOTES, PREVIOUS_BALANCED_MORNING_NOTES,
                          PREVIOUS_REVIEW_MORNING_NOTES, PREVIOUS_BRIEFING_MORNING_NOTES,
                          PREVIOUS_FLEXIBLE_MORNING_NOTES, MORNING_NOTES):
            item.notes = MORNING_NOTES
            item.completion_requirement = "morning_plan"
        item.revision += 1
        item.updated_at = now
    recurrence = {"frequency": "daily", "time": "21:00", "timezone": state.timezone}
    contact = db.scalar(select(AssistantAgendaItem).where(
        AssistantAgendaItem.title == "Daily check-in", AssistantAgendaItem.kind == "task",
        AssistantAgendaItem.recurrence_json.is_not(None),
    ).order_by(AssistantAgendaItem.id).limit(1))
    if contact is None:
        db.add(AssistantAgendaItem(id=next_item_id(db), title="Daily check-in", kind="task",
            notes=DAILY_INTERACTION_NOTES, recurrence_json=recurrence, recurrence_start_at=now,
            due_at=next_at(recurrence, now), completion_requirement="interaction_today"))
    else:
        contact.completion_requirement = "interaction_today"
        contact.revision += 1
        contact.updated_at = now
    state.planning_contract_migrated = True
    db.commit()


def daily_interaction_status(db, timezone: str, now: datetime) -> dict:
    """Count actual Assistant output, including canonical archived message bodies."""
    from app.services.agent_memory_service import AgentMemoryService

    zone = ZoneInfo(timezone)
    day = now.astimezone(zone).date()
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    sessions = select(ActSession.id).where(ActSession.agent_id == "assistant")
    turns = list(db.scalars(select(ActTurn).where(
        ActTurn.session_id.in_(sessions), ActTurn.status == "succeeded",
        ActTurn.backend_message_kind.is_(None), ActTurn.completed_at >= start, ActTurn.completed_at < end,
    )))
    live = list(db.scalars(select(ActLiveMessage).join(ActTurn).where(
        ActTurn.session_id.in_(sessions), ActLiveMessage.role == "assistant",
        ActLiveMessage.created_at >= start, ActLiveMessage.created_at < end,
    )))
    memory = AgentMemoryService(db)
    content = {}

    def read(turn):
        if turn.id not in content:
            content[turn.id] = memory.read_turn_content(turn, agent_id="assistant")
        return content[turn.id]

    timestamps = [turn.completed_at for turn in turns if (read(turn)["assistant_message"] or "").strip()]
    timestamps += [message.created_at for message in live
                   if read(db.get(ActTurn, message.turn_id))["live_messages"].get(message.id, "").strip()]
    timestamps = [stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC) for stamp in timestamps]
    return {"day": day.isoformat(), "timezone": timezone, "required": 1,
            "count": len(timestamps), "satisfied": bool(timestamps),
            "last_interaction_at": max(timestamps) if timestamps else None}


def daily_interaction_error(db, turn: ActTurn, now: datetime) -> str | None:
    """Verify observable recurring outcomes; semantic quality remains model judgment."""
    # MCP tools run in another process. Refresh only their metadata after
    # flushing the pending final output; keep the dispatcher's response intact.
    db.flush()
    db.refresh(turn, attribute_names=["assistant_context_json", "assistant_review_json"])
    activation = turn.assistant_context_json or {}
    occurrence = activation.get("occurrence")
    if not occurrence:
        return None
    requirement = activation.get("completion_requirement")
    if requirement == "morning_plan":
        from app.services.assistant_daily_plan import morning_plan_error

        return morning_plan_error(db, turn, now)
    if requirement == "interaction_today":
        state = db.get(AssistantState, 1)
        timezone = state.timezone if state else occurrence["timezone"]
        if not daily_interaction_status(db, timezone, now)["satisfied"]:
            return "Daily interaction is incomplete: produce a meaningful user-visible message or agenda question."
    return None
