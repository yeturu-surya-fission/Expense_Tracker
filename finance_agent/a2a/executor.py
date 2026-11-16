"""
The bridge between the A2A protocol and our skills: an incoming message becomes (or continues) a task, the skill runs, and
its result becomes the task's next state:

    completed       the skill is done                         (the reply carries text + structured data)
    input-required  we need an answer from the human          (the caller asks them and continues the SAME task)
    failed          a plain-words problem
"""
from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater

from finance_agent.a2a import skills
from finance_agent.llm import LLM


class FinanceExecutor(AgentExecutor):
    def __init__(self, llm: LLM):
        self.llm = llm

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        def say(text: str, data: dict | None = None):
            parts = [new_text_part(text)] + ([new_data_part(data)] if data else [])
            return updater.new_agent_message(parts)

        claims = context.call_context.state.get("claims")
        if not claims:
            await updater.failed(say("You aren't signed in."))
            return
        await updater.start_work()
        payload = next((d for d in get_data_parts(context.message.parts) if isinstance(d, dict)), {})
        caller = skills.Caller(ref=str(claims["sub"]).removeprefix("user:"), name=str(claims.get("name") or "Employee"))
        result = await skills.run(self.llm, caller, payload, task.id, get_message_text(context.message))
        message = say(result.text, result.data)
        if result.state == "input_required":
            await updater.requires_input(message)
        elif result.state == "failed":
            await updater.failed(message)
        else:
            await updater.complete(message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()
