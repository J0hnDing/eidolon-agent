"""Built-in agent dispatcher ownership, independent of transport adapters."""

from app.services.act_turn_dispatcher import ActTurnDispatcher, act_turn_dispatcher
from app.services.assistant_turn_dispatcher import assistant_turn_dispatcher

agent_dispatchers = {
    "act": act_turn_dispatcher,
    "observer": ActTurnDispatcher("observer"),
    "assistant": assistant_turn_dispatcher,
}
