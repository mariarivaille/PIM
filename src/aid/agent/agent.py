import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field

from aid.agent.prompts import build_system_prompt
from aid.agent.tools import TOOLS, ToolContext, run_tool, task_view
from aid.core.models import Task, User, utcnow
from aid.core.repository import Repository
from aid.core.timeutils import to_local
from aid.llm.base import LLMProvider, Message

log = logging.getLogger(__name__)

MAX_STEPS = 6


@dataclass
class AgentReply:
    text: str
    touched: list[Task] = field(default_factory=list)


class History:
    """Короткая память диалога в процессе (только текст user/assistant).

    TODO: вынести в БД, когда понадобится переживать рестарты / несколько инстансов.
    """

    def __init__(self, size: int):
        self._data: dict[int, deque[Message]] = defaultdict(lambda: deque(maxlen=size))

    def get(self, user_id: int) -> list[Message]:
        return list(self._data[user_id])

    def add(self, user_id: int, *messages: Message) -> None:
        self._data[user_id].extend(messages)

    def clear(self, user_id: int) -> None:
        self._data.pop(user_id, None)


class Agent:
    def __init__(self, llm: LLMProvider, repo: Repository, history: History):
        self._llm = llm
        self._repo = repo
        self.history = history
        self._specs = [spec for spec, _ in TOOLS.values()]

    async def handle(self, user: User, text: str) -> AgentReply:
        ctx = ToolContext(repo=self._repo, user=user)
        tasks = await self._repo.list_open_tasks(user.id)
        tasks_block = "\n".join(json.dumps(task_view(t, user.timezone), ensure_ascii=False) for t in tasks)
        system = Message("system", build_system_prompt(to_local(utcnow(), user.timezone), user.timezone, tasks_block))

        user_msg = Message("user", text)
        messages = [system, *self.history.get(user.id), user_msg]

        reply = ""
        for _ in range(MAX_STEPS):
            answer = await self._llm.chat(messages, self._specs)
            messages.append(answer)
            if not answer.tool_calls:
                reply = answer.content
                break
            for call in answer.tool_calls:
                result = await run_tool(ctx, call.name, call.arguments)
                log.info("tool %s(%s) -> %s", call.name, call.arguments, result)
                messages.append(
                    Message("tool", json.dumps(result, ensure_ascii=False), tool_call_id=call.id, name=call.name)
                )
        else:
            reply = "Не получилось довести дело до конца, попробуй переформулировать."

        reply = reply.strip() or "Готово."
        self.history.add(user.id, user_msg, Message("assistant", reply))
        return AgentReply(text=reply, touched=ctx.touched)
